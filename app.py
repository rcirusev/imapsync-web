"""
imapsync Web UI — real backend.

Runs the actual `imapsync` binary (must be installed and on PATH — see
install.sh / README.md) for each migration, streams its real stdout to the
browser over Server-Sent Events, stores every run in a small SQLite history
so the same install can be reused for many migrations by many people.

Supports both a single migration (New migration tab) and a bulk migration
from a CSV file (Bulk tab) — the two share the exact same job runner, they
just differ in how the job dicts are produced (one form submit vs. one CSV
row each) and in how many run back-to-back.

Environment variables:
  IMAPSYNC_WEB_DATA_DIR   Where the SQLite DB + per-job logs live.
                          Defaults to ./data next to this file.
  IMAPSYNC_BIN            Explicit path to the imapsync binary, if it's not
                          on PATH.
  IMAPSYNC_WEB_USERNAME,
  IMAPSYNC_WEB_PASSWORD   Turn on HTTP Basic Auth for the whole app when
                          BOTH are set. Left unset, the app stays open to
                          anyone who can reach it — the same as before this
                          existed. See README's Security notes.
"""

import fcntl
import hmac
import json
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

MAX_BATCH_CONCURRENCY = 25

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

import bulk_csv
import conn_test
import crypto_store
import db
import imapsync_runner as runner

app = Flask(__name__)


def _asset_version(filename):
    """Mtime of a static file, used as a cache-busting query string so a
    browser that already cached the old app.js/style.css from a previous
    deploy is forced to fetch the new one instead of silently keeping
    stale JS/CSS after `docker compose pull && up -d` ships a fix."""
    path = os.path.join(app.static_folder, filename)
    try:
        return str(int(os.path.getmtime(path)))
    except OSError:
        return "0"


@app.context_processor
def _inject_asset_version():
    return {"asset_version": _asset_version}


DATA_DIR = os.environ.get("IMAPSYNC_WEB_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data"
)
DB_PATH = os.path.join(DATA_DIR, "imapsync-web.sqlite3")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
db.init_db(DB_PATH)

# install.sh/Dockerfile run gunicorn with multiple worker PROCESSES (2 by
# default), each of which imports this module — and therefore, without this
# guard, each of which would independently run the one-time startup sweep
# below. That's actively dangerous, not just wasteful: two workers booting
# at once can both see the same "interrupted" batch and each launch their
# own resume of it, or worse, one worker's brand-new resumed batch (rows
# briefly "queued" the instant they're created) can get caught by a
# *sibling* worker's own orphan sweep — which has no way to tell "genuinely
# stale from before this boot" apart from "a sibling worker created this a
# moment ago" — triggering another auto-resume on top of it, cascading
# ("batch (auto-resumed) (auto-resumed)...").
#
# An advisory, non-blocking flock on a marker file means only the first
# worker to reach this line actually runs the sweep; every other worker's
# flock call fails immediately and it skips straight past — there's
# nothing for it to wait for, the winner already has it covered. The lock
# is deliberately never released: it lives exactly as long as the winning
# worker process does, and a future gunicorn restart starts this whole
# race fresh (the OS drops the flock the instant that process exits).
_startup_lock_fp = open(os.path.join(DATA_DIR, ".startup.lock"), "w")
try:
    fcntl.flock(_startup_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    WON_STARTUP_RACE = True
except OSError:
    WON_STARTUP_RACE = False

if WON_STARTUP_RACE:
    db.mark_orphaned_running_as_interrupted(DB_PATH)
    db.mark_orphaned_batches_as_interrupted(DB_PATH)

crypto_store.init(DATA_DIR)

IMAPSYNC_BIN = os.environ.get("IMAPSYNC_BIN") or runner.find_imapsync_binary()

# ---------------------------------------------------------------------------
# Optional HTTP Basic Auth — off unless both env vars are set, so existing
# installs behave exactly as before until someone opts in. See README's
# "Add authentication" section for how to set these via systemd.
# ---------------------------------------------------------------------------
BASIC_AUTH_USER = os.environ.get("IMAPSYNC_WEB_USERNAME")
BASIC_AUTH_PASS = os.environ.get("IMAPSYNC_WEB_PASSWORD")
BASIC_AUTH_ENABLED = bool(BASIC_AUTH_USER and BASIC_AUTH_PASS)


@app.before_request
def _require_basic_auth():
    if not BASIC_AUTH_ENABLED:
        return None
    auth = request.authorization
    ok = (
        auth is not None
        and hmac.compare_digest(auth.username or "", BASIC_AUTH_USER)
        and hmac.compare_digest(auth.password or "", BASIC_AUTH_PASS)
    )
    if not ok:
        return Response(
            "Authentication required.", 401,
            {"WWW-Authenticate": 'Basic realm="imapsync-web"'},
        )
    return None


# ---------------------------------------------------------------------------
# CSRF hardening. Browsers re-attach cached HTTP Basic Auth credentials to
# same-origin requests even when a third-party page initiates them, so
# Basic Auth alone does not stop a malicious page from silently POSTing to
# this app in an operator's browser (clear history, stop a batch, fire a
# schedule, ...). Every state-changing request must carry this header. It
# has no secret value — its only purpose is that a plain cross-site <form>
# submit, or a "simple" cross-origin fetch/XHR, cannot attach a custom
# header without first triggering a CORS preflight, which this app never
# answers with an Access-Control-Allow-* response. The frontend
# (static/js/app.js) sends this header on every non-GET request.
# ---------------------------------------------------------------------------
@app.before_request
def _require_csrf_header():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.headers.get("X-Requested-With") != "imapsync-web":
        return jsonify({"error": "Missing or invalid CSRF header."}), 403
    return None


# key -> {"lines": [(event, data), ...], "subscribers": [Queue, ...], "done": bool}
# ACTIVE is keyed by job_id (one entry per migration, single or bulk-row).
# BATCHES is keyed by batch_id and only carries control events (row_start /
# row_done / batch_done) — the per-row log itself still streams through the
# matching ACTIVE[job_id] exactly like a single migration would.
ACTIVE = {}
ACTIVE_LOCK = threading.Lock()
BATCHES = {}
BATCHES_LOCK = threading.Lock()

# batch_ids with a pending Stop request. Checked by _run_batch_thread before
# starting each new row — a row not yet started is skipped outright; a row
# already in flight is separately killed via RUNNING_PROCESSES below rather
# than left to finish on its own.
BATCH_CANCEL_REQUESTS = set()
BATCH_CANCEL_LOCK = threading.Lock()

# job_id -> the live subprocess.Popen running that job's imapsync, for as
# long as it's running — lets Stop actually terminate an in-flight row
# instead of only preventing rows that haven't started yet. Killing
# mid-transfer is safe to resume from for the same reason a crash
# mid-transfer already is: imapsync itself is incremental.
RUNNING_PROCESSES = {}
RUNNING_PROCESSES_LOCK = threading.Lock()

# job_ids _execute_job should report as "interrupted" (with a clear "user
# stopped this" message) rather than "error" once their subprocess exits —
# set by _kill_running_job right before it signals the process, since a
# killed process's exit looks, from the outside, just like any other
# nonzero-exit failure.
KILL_REQUESTED = set()
KILL_REQUESTED_LOCK = threading.Lock()


def _signal_process_group(proc, sig):
    """Signals proc's whole process group (it was started with
    start_new_session=True — see imapsync_runner.run_imapsync — specifically
    so this reaches any child it may have shelled out to, not just proc
    itself), falling back to signaling just the one PID if the group is
    already gone (a harmless race with the process exiting on its own)."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except ProcessLookupError:
        pass


def _kill_running_job(job_id):
    """Terminates job_id's live imapsync subprocess (and its whole process
    group), if it's still running. SIGTERM first; escalates to SIGKILL
    after a grace period if it doesn't exit on its own. Returns True if a
    running process was found and signaled, False if this job wasn't (or
    is no longer) running."""
    with RUNNING_PROCESSES_LOCK:
        proc = RUNNING_PROCESSES.get(job_id)
    if not proc or proc.poll() is not None:
        return False

    with KILL_REQUESTED_LOCK:
        KILL_REQUESTED.add(job_id)
    _signal_process_group(proc, signal.SIGTERM)

    def escalate():
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _signal_process_group(proc, signal.SIGKILL)

    threading.Thread(target=escalate, daemon=True).start()
    return True


def _broadcast(registry, lock, key, event, data):
    with lock:
        state = registry.get(key)
        if not state:
            return
        state["lines"].append((event, data))
        for q in state["subscribers"]:
            q.put((event, data))


def _close(registry, lock, key):
    with lock:
        state = registry.get(key)
        if state:
            state["done"] = True
            for q in state["subscribers"]:
                q.put(("__close__", {}))


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _stream_live(registry, lock, key):
    """Shared SSE tail: replay whatever already happened, then follow live."""
    with lock:
        state = registry.get(key)
        if state is None or state["done"]:
            return None
        q = queue.Queue()
        for event, data in state["lines"]:
            q.put((event, data))
        state["subscribers"].append(q)

    def generate():
        while True:
            event, data = q.get()
            if event == "__close__":
                break
            yield _sse(event, data)

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


def _new_job_dict(row_or_payload, job_id=None):
    """Build a job dict (as imapsync_runner expects) from either the single-
    migration JSON payload or one parsed bulk CSV row — both already share
    the same shape. Pass job_id to pin a specific id (bulk_start assigns
    ids up front, before its background thread starts, so it can store
    scheduled-delta-sync credentials against the exact id each row will
    use)."""
    job_id = job_id or str(uuid.uuid4())
    p = row_or_payload
    return {
        "id": job_id,
        "created_at": time.time(),
        "host1": p["host1"].strip(),
        "port1": str(p.get("port1") or "993"),
        "ssl1": bool(p.get("ssl1", True)),
        "user1": p["user1"].strip(),
        "authuser1": (p.get("authuser1") or "").strip() or None,
        "host2": p["host2"].strip(),
        "port2": str(p.get("port2") or "993"),
        "ssl2": bool(p.get("ssl2", True)),
        "user2": p["user2"].strip(),
        "authuser2": (p.get("authuser2") or "").strip() or None,
        "options": p.get("options", {}),
        "log_path": os.path.join(LOGS_DIR, f"{job_id}.log"),
    }


def _execute_job(job, password1, password2):
    """
    Runs one real imapsync job to completion: marks it started, streams its
    output into ACTIVE[job_id], persists the result to the DB, and returns
    the final status ("success" | "error" | "interrupted"). Used by both a
    lone migration and each row of a bulk batch.
    """
    job_id = job["id"]
    db.mark_started(DB_PATH, job_id, time.time())

    def on_line(line):
        _broadcast(ACTIVE, ACTIVE_LOCK, job_id, "log", {"line": line})

    def on_process(proc):
        with RUNNING_PROCESSES_LOCK:
            RUNNING_PROCESSES[job_id] = proc

    log_path = job["log_path"]
    # Append, not overwrite: a retry reuses this same job id (and so this
    # same log file — see db.reset_job_for_retry), so a previous attempt's
    # output stays readable as part of one continuous story instead of
    # being silently discarded. A brand new job's log file doesn't exist
    # yet, so this is identical to starting fresh either way.
    is_retry = os.path.isfile(log_path) and os.path.getsize(log_path) > 0

    try:
        with open(log_path, "a") as log_fp:
            if is_retry:
                marker = f"--- Resumed {time.strftime('%Y-%m-%d %H:%M:%S')} ---"
                log_fp.write(marker + "\n")
                log_fp.flush()
                on_line(marker)
            returncode, full_text, elapsed = runner.run_imapsync(
                IMAPSYNC_BIN, job, password1, password2, log_fp, on_line, on_process=on_process
            )

        with KILL_REQUESTED_LOCK:
            was_killed = job_id in KILL_REQUESTED
            KILL_REQUESTED.discard(job_id)

        if was_killed:
            # Deliberately terminated via Stop — report it as such instead
            # of running it through parse_summary, whose best-effort log
            # parsing has no idea "nonzero exit" here means "we killed it",
            # not "imapsync failed on its own".
            db.finish_job(
                DB_PATH, job_id, status="interrupted", finished_at=time.time(),
                folders=None, messages=None, data_mb=None, errors=None,
                duration_s=round(elapsed, 1), return_code=returncode,
                error_message="Stopped by user request.",
            )
            _broadcast(ACTIVE, ACTIVE_LOCK, job_id, "done", {
                "status": "interrupted", "duration_s": round(elapsed, 1),
            })
            return "interrupted"

        summary = runner.parse_summary(full_text, returncode)
        db.finish_job(
            DB_PATH, job_id,
            status=summary["status"], finished_at=time.time(),
            folders=summary["folders"], messages=summary["messages"],
            data_mb=summary["data_mb"], errors=summary["errors"],
            duration_s=round(elapsed, 1), return_code=returncode,
        )
        _broadcast(ACTIVE, ACTIVE_LOCK, job_id, "done", {
            "status": summary["status"], "folders": summary["folders"],
            "messages": summary["messages"], "data_mb": summary["data_mb"],
            "errors": summary["errors"], "duration_s": round(elapsed, 1),
        })
        return summary["status"]
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        db.finish_job(
            DB_PATH, job_id, status="error", finished_at=time.time(),
            folders=None, messages=None, data_mb=None, errors=None,
            duration_s=None, return_code=None, error_message=str(exc),
        )
        _broadcast(ACTIVE, ACTIVE_LOCK, job_id, "log", {"line": f"ERROR: {exc}"})
        _broadcast(ACTIVE, ACTIVE_LOCK, job_id, "done", {"status": "error", "error": str(exc)})
        return "error"
    finally:
        with RUNNING_PROCESSES_LOCK:
            RUNNING_PROCESSES.pop(job_id, None)
        with KILL_REQUESTED_LOCK:
            KILL_REQUESTED.discard(job_id)
        _close(ACTIVE, ACTIVE_LOCK, job_id)


def _enable_delta_sync(kind, ref_id, interval_hours, credential_pairs):
    """
    Turns on scheduled delta sync for a job (kind='job') or a whole bulk
    batch (kind='batch'). credential_pairs is a list of
    (job_id, password1, password2) — one pair for a single job, one per row
    for a batch — encrypted and stored in credential_vault keyed by each
    job_id, plus one schedules row referencing ref_id (the job's or batch's
    own id) that the scheduler loop checks against the clock.
    """
    now = time.time()
    for job_id, password1, password2 in credential_pairs:
        db.store_credentials(
            DB_PATH, job_id,
            crypto_store.encrypt(password1), crypto_store.encrypt(password2), now,
        )
    db.create_schedule(DB_PATH, str(uuid.uuid4()), kind, ref_id, interval_hours, now)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/imapsync-status")
def imapsync_status():
    return jsonify({"available": bool(IMAPSYNC_BIN), "path": IMAPSYNC_BIN})


# ---------------------------------------------------------------------------
# Single migration
# ---------------------------------------------------------------------------

@app.route("/api/start", methods=["POST"])
def start():
    if not IMAPSYNC_BIN:
        return jsonify({
            "error": "imapsync binary not found on this server. Run install.sh, "
                     "or set IMAPSYNC_BIN, then restart the service."
        }), 503

    payload = request.get_json(force=True) or {}
    for required in ("host1", "user1", "host2", "user2"):
        if not (payload.get(required) or "").strip():
            return jsonify({"error": f"Missing required field: {required}"}), 400

    job = _new_job_dict(payload)
    db.create_job(DB_PATH, job)
    with ACTIVE_LOCK:
        ACTIVE[job["id"]] = {"lines": [], "subscribers": [], "done": False}

    password1 = payload.get("password1") or ""
    password2 = payload.get("password2") or ""

    schedule = payload.get("schedule") or {}
    if schedule.get("enabled"):
        interval_hours = float(schedule.get("interval_hours") or 0)
        if interval_hours > 0:
            _enable_delta_sync("job", job["id"], interval_hours, [(job["id"], password1, password2)])

    threading.Thread(
        target=_execute_job, args=(job, password1, password2), daemon=True
    ).start()

    return jsonify({"job_id": job["id"]})


@app.route("/api/jobs/<job_id>/retry", methods=["POST"])
def job_retry(job_id):
    """
    Re-runs a single interrupted/failed job — same id, same log file
    (appended to, not overwritten — see _execute_job) — instead of
    creating a new job row, so History keeps one row per mailbox across
    retries. The single-job equivalent of a batch's "Retry rows" (see
    batch_retry): host/port/SSL/username/options are pulled back out of
    this job's own DB record; password falls back to a still-stored
    "auto-resume" credential (see has_stored_password on /api/jobs) if the
    client doesn't supply one, same as any other password field in this
    app otherwise — sent once, never written to a file.
    """
    if not IMAPSYNC_BIN:
        return jsonify({
            "error": "imapsync binary not found on this server. Run install.sh, "
                     "or set IMAPSYNC_BIN, then restart the service."
        }), 503

    original = db.get_job(DB_PATH, job_id)
    if not original:
        return jsonify({"error": "Unknown job id."}), 404
    if original["status"] not in ("error", "interrupted"):
        return jsonify({"error": "Job is not in a retryable state."}), 400

    payload = request.get_json(silent=True) or {}
    password1 = (payload.get("password1") or "").strip()
    password2 = (payload.get("password2") or "").strip()
    if not password1 or not password2:
        creds = db.get_credentials(DB_PATH, job_id)
        if creds:
            password1 = password1 or crypto_store.decrypt(creds["enc_password1"])
            password2 = password2 or crypto_store.decrypt(creds["enc_password2"])
    if not password1 or not password2:
        return jsonify({"error": "Missing password(s)."}), 400

    options = json.loads(original["options_json"] or "{}")

    if original.get("batch_id"):
        # This row belongs to a bulk batch — route through the same
        # _retry_batch_rows machinery "Retry rows" uses, instead of just
        # _execute_job-ing it directly, so the BATCH's own status/progress
        # stay in sync too. Retrying only the job and leaving the batch
        # row untouched left it showing its old, stale status (e.g. still
        # "Interrupted") in the Bulk batches table — no "Running" state,
        # so no Stop button, even while this row was actively running.
        batch = db.get_batch(DB_PATH, original["batch_id"])
        if not batch:
            return jsonify({"error": "This row's batch no longer exists."}), 404
        _retry_batch_rows(batch, [{
            "host1": original["host1"], "port1": original["port1"], "ssl1": bool(original["ssl1"]),
            "user1": original["user1"], "authuser1": original.get("authuser1") or None, "password1": password1,
            "host2": original["host2"], "port2": original["port2"], "ssl2": bool(original["ssl2"]),
            "user2": original["user2"], "authuser2": original.get("authuser2") or None, "password2": password2,
            "options": options, "_job_id": job_id,
        }], keep_passwords=False)
        return jsonify({"job_id": job_id, "batch_id": batch["id"]})

    job = _new_job_dict({
        "host1": original["host1"], "port1": original["port1"], "ssl1": bool(original["ssl1"]),
        "user1": original["user1"], "authuser1": original.get("authuser1") or None,
        "host2": original["host2"], "port2": original["port2"], "ssl2": bool(original["ssl2"]),
        "user2": original["user2"], "authuser2": original.get("authuser2") or None,
        "options": options,
    }, job_id=job_id)
    db.reset_job_for_retry(DB_PATH, job_id)
    with ACTIVE_LOCK:
        ACTIVE[job_id] = {"lines": [], "subscribers": [], "done": False}

    threading.Thread(target=_execute_job, args=(job, password1, password2), daemon=True).start()

    # Used for this attempt — redundant now, same as batch_retry.
    db.delete_credentials(DB_PATH, job_id)

    return jsonify({"job_id": job_id})


@app.route("/api/stream/<job_id>")
def stream(job_id):
    live_response = _stream_live(ACTIVE, ACTIVE_LOCK, job_id)
    if live_response is not None:
        return live_response

    # Job already finished (or predates this process) — replay from disk + DB.
    job = db.get_job(DB_PATH, job_id)
    if not job:
        return Response(_sse("log", {"line": "Unknown job id."}), mimetype="text/event-stream")

    def generate_replay():
        log_path = os.path.join(LOGS_DIR, f"{job_id}.log")
        if os.path.isfile(log_path):
            with open(log_path) as f:
                for line in f:
                    yield _sse("log", {"line": line.rstrip("\n")})
        yield _sse("done", {
            "status": job["status"], "folders": job["folders"], "messages": job["messages"],
            "data_mb": job["data_mb"], "errors": job["errors"], "duration_s": job["duration_s"],
        })

    return Response(stream_with_context(generate_replay()), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# Bulk migration (CSV)
# ---------------------------------------------------------------------------

def _run_batch_thread(batch_id, rows, schedule_id=None, max_concurrent=1):
    """
    Runs a round of bulk-batch rows through a bounded worker pool sized
    max_concurrent instead of strictly one-at-a-time, so a large CSV can use
    spare capacity on the source/destination servers instead of leaving a
    job idle while the previous one finishes. max_concurrent=1 (the
    default) reproduces the old fully-sequential behavior exactly.

    `rows` must already have a DB job row apiece — either the batch's very
    first run (every row, freshly "queued" — see bulk_start) or a retry of
    some existing subset reusing its rows in place (see
    db.reset_job_for_retry / _retry_batch_rows) — this function only ever
    updates rows that already exist, never creates one. Because of that,
    the batch's persisted completed/success/error counts are recomputed
    from the DB after every row (db.recompute_batch_progress) rather than
    tracked incrementally from 0: an incremental counter would be wrong for
    a partial retry, since it has no way to know about rows that already
    settled in a previous round.
    """
    round_total = len(rows)
    stopped_event = threading.Event()

    def run_row(index, row):
        job = _new_job_dict(row, job_id=row.get("_job_id"))

        with BATCH_CANCEL_LOCK:
            if batch_id in BATCH_CANCEL_REQUESTS:
                stopped_event.set()
        if stopped_event.is_set():
            # A Stop request landed before this row got its turn — leave it
            # as a normal "interrupted" row (same as a server crash would)
            # instead of silently vanishing, so "Retry rows"/"Download
            # failed CSV" still pick it up.
            db.finish_job(
                DB_PATH, job["id"], status="interrupted", finished_at=time.time(),
                folders=None, messages=None, data_mb=None, errors=None, duration_s=None,
                return_code=None, error_message="Batch was stopped before this row started.",
            )
            db.recompute_batch_progress(DB_PATH, batch_id)
            return

        with ACTIVE_LOCK:
            ACTIVE[job["id"]] = {"lines": [], "subscribers": [], "done": False}

        _broadcast(BATCHES, BATCHES_LOCK, batch_id, "row_start", {
            "index": index, "total": round_total, "job_id": job["id"],
            "host1": job["host1"], "user1": job["user1"],
            "host2": job["host2"], "user2": job["user2"],
        })

        status = _execute_job(job, row["password1"], row["password2"])

        # Persisted after every row (not just at the end) so the batch's
        # progress survives a page reload, a dropped connection, or even
        # this process dying partway through — History always shows how
        # far a bulk run actually got, not just whether it finished.
        db.recompute_batch_progress(DB_PATH, batch_id)

        finished = db.get_job(DB_PATH, job["id"])
        _broadcast(BATCHES, BATCHES_LOCK, batch_id, "row_done", {
            "index": index, "total": round_total, "job_id": job["id"],
            "host1": job["host1"], "user1": job["user1"],
            "host2": job["host2"], "user2": job["user2"],
            "status": finished["status"], "messages": finished["messages"],
            "errors": finished["errors"], "duration_s": finished["duration_s"],
        })

    with ThreadPoolExecutor(max_workers=max(1, max_concurrent)) as pool:
        futures = [pool.submit(run_row, index, row) for index, row in enumerate(rows, start=1)]
        for future in futures:
            future.result()  # propagate a worker crash instead of swallowing it

    with BATCH_CANCEL_LOCK:
        # Membership alone (not just stopped_event, which only fires for a
        # row skipped *before* it started) also covers the case where Stop
        # arrived while every row was already in flight and got killed
        # in-place (see bulk_stop -> _kill_running_job) — that batch was
        # just as deliberately stopped, and should show "Stopped", not
        # "Done", even though stopped_event never had a not-yet-started row
        # to catch.
        stop_was_requested = batch_id in BATCH_CANCEL_REQUESTS
        BATCH_CANCEL_REQUESTS.discard(batch_id)
    stopped_early = stopped_event.is_set() or stop_was_requested

    if stopped_early:
        db.stop_batch(DB_PATH, batch_id, time.time())
    else:
        db.finish_batch(DB_PATH, batch_id, time.time())

    # This batch is done (one way or another) — drop "auto-resume if the
    # server restarts mid-batch" credentials for rows that actually
    # succeeded (nothing left to ever retry, so nothing left to resume).
    # Rows still interrupted/errored — including one Stop just killed —
    # keep theirs: a future crash before anyone gets to it can still
    # auto-resume it, and "Retry rows" (see batch_retry) can reuse it
    # instead of making someone retype a password that's already sitting
    # there encrypted for exactly this. Skipped entirely if an active
    # delta-sync schedule references this exact batch_id — that schedule
    # needs every row's credentials kept indefinitely regardless of that
    # row's last outcome.
    if not db.get_schedule_by_ref(DB_PATH, "batch", batch_id):
        job_ids = {row["_job_id"] for row in rows if row.get("_job_id")}
        successful_job_ids = [
            job["id"] for job in db.list_jobs_by_batch(DB_PATH, batch_id)
            if job["id"] in job_ids and job["status"] == "success"
        ]
        db.delete_credentials_many(DB_PATH, successful_job_ids)

    _, success, error = db.recompute_batch_progress(DB_PATH, batch_id)
    batch_total = (db.get_batch(DB_PATH, batch_id) or {}).get("total", round_total)
    _broadcast(BATCHES, BATCHES_LOCK, batch_id, "batch_done", {
        "total": batch_total, "success": success, "error": error, "stopped": stopped_early,
    })
    _close(BATCHES, BATCHES_LOCK, batch_id)


def _retry_batch_rows(batch, rows, keep_passwords):
    """
    Shared by the "Retry rows" endpoint and the startup auto-resume sweep:
    re-runs a chosen subset of an existing batch's rows IN PLACE — same
    batch id, same job id per row (each row's "_job_id" here is its
    existing id, not a new one) — instead of creating a new batch, so
    History keeps one row per mailbox/batch that gets updated across
    retries rather than growing with every attempt. If keep_passwords,
    stores/refreshes each row's "auto-resume if the server restarts
    mid-batch" credential the same way bulk_start does.
    """
    if not rows:
        return False

    max_concurrent = min(max(int(batch.get("max_concurrent") or 1), 1), MAX_BATCH_CONCURRENCY)
    now = time.time()
    for row in rows:
        db.reset_job_for_retry(DB_PATH, row["_job_id"])
        if keep_passwords:
            db.store_credentials(
                DB_PATH, row["_job_id"],
                crypto_store.encrypt(row["password1"]), crypto_store.encrypt(row["password2"]), now,
            )
    db.reopen_batch_for_retry(DB_PATH, batch["id"])
    # Reflect the just-reset rows immediately rather than leaving the
    # batch's completed/success/error at their stale pre-retry values
    # until the first row actually finishes (a few seconds away for a
    # slow migration) — matters most right after this call returns, since
    # that's when the Bulk batches table gets its next look.
    db.recompute_batch_progress(DB_PATH, batch["id"])
    with BATCHES_LOCK:
        BATCHES[batch["id"]] = {"lines": [], "subscribers": [], "done": False}

    threading.Thread(
        target=_run_batch_thread, args=(batch["id"], rows),
        kwargs={"max_concurrent": max_concurrent}, daemon=True,
    ).start()
    return True


def _launch_staged_batch(batch):
    """
    Runs a batch that has been sitting staged (see bulk_start's `stage`
    flag). Its rows' host/user/options come from the job rows written at
    upload time and their passwords from the credential vault — the only
    place they could live while the batch waited, since the uploaded CSV is
    never written to disk.

    The caller must already have won db.claim_staged_batch for this batch,
    so both the Start button and the scheduler's due sweep can call this
    without either being able to start the same batch twice.
    """
    jobs = db.list_jobs_by_batch(DB_PATH, batch["id"])
    rows = []
    for job in jobs:
        creds = db.get_credentials(DB_PATH, job["id"])
        if not creds:
            continue  # nothing to log in with — skip rather than fail the whole batch
        rows.append({
            "host1": job["host1"], "port1": job["port1"], "ssl1": bool(job["ssl1"]),
            "user1": job["user1"], "authuser1": job.get("authuser1") or None,
            "password1": crypto_store.decrypt(creds["enc_password1"]),
            "host2": job["host2"], "port2": job["port2"], "ssl2": bool(job["ssl2"]),
            "user2": job["user2"], "authuser2": job.get("authuser2") or None,
            "password2": crypto_store.decrypt(creds["enc_password2"]),
            "options": json.loads(job["options_json"] or "{}"),
            "_job_id": job["id"],  # reused, never a fresh id — see _retry_batch_rows
        })

    if not rows:
        # claim_staged_batch has already flipped this batch to 'running', so
        # bailing out silently would strand it there forever. Only reachable
        # if the vault entries went missing under it (a manual DB edit, say)
        # — there is nothing to log in with, so call it stopped and move on.
        db.stop_batch(DB_PATH, batch["id"], time.time())
        return False

    max_concurrent = min(max(int(batch.get("max_concurrent") or 1), 1), MAX_BATCH_CONCURRENCY)
    for row in rows:
        db.reset_job_for_retry(DB_PATH, row["_job_id"])  # 'staged' -> 'queued'
    db.recompute_batch_progress(DB_PATH, batch["id"])
    with BATCHES_LOCK:
        BATCHES[batch["id"]] = {"lines": [], "subscribers": [], "done": False}

    threading.Thread(
        target=_run_batch_thread, args=(batch["id"], rows),
        kwargs={"max_concurrent": max_concurrent}, daemon=True,
    ).start()
    return True


@app.route("/api/batches/<batch_id>/start", methods=["POST"])
def batch_start(batch_id):
    if not IMAPSYNC_BIN:
        return jsonify({
            "error": "imapsync binary not found on this server. Run install.sh, "
                     "or set IMAPSYNC_BIN, then restart the service."
        }), 503

    batch = db.get_batch(DB_PATH, batch_id)
    if not batch:
        return jsonify({"error": "Unknown batch id."}), 404
    if batch["status"] != "staged":
        return jsonify({"error": "This batch isn't staged — it has already been started."}), 409
    if not db.claim_staged_batch(DB_PATH, batch_id):
        return jsonify({"error": "This batch has just been started elsewhere."}), 409

    if not _launch_staged_batch(batch):
        return jsonify({"error": "This batch has no rows left to run."}), 400
    return jsonify({"batch_id": batch_id, "started": True})


@app.route("/api/batches/<batch_id>", methods=["DELETE"])
def batch_delete(batch_id):
    """Discards a staged batch outright, credentials included. Only staged
    batches — anything that actually ran belongs in history, where Clear
    history decides when it goes."""
    batch = db.get_batch(DB_PATH, batch_id)
    if not batch:
        return jsonify({"error": "Unknown batch id."}), 404
    if batch["status"] != "staged":
        return jsonify({"error": "Only a staged batch can be discarded."}), 409

    for path in db.delete_batch(DB_PATH, batch_id):
        try:
            os.remove(path)
        except OSError:
            pass
    with BATCHES_LOCK:
        BATCHES.pop(batch_id, None)
    return jsonify({"batch_id": batch_id, "deleted": True})


@app.route("/api/bulk/template")
def bulk_template():
    return Response(
        bulk_csv.build_template_csv(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=imapsync-bulk-template.csv"},
    )


@app.route("/api/bulk/start", methods=["POST"])
def bulk_start():
    if not IMAPSYNC_BIN:
        return jsonify({
            "error": "imapsync binary not found on this server. Run install.sh, "
                     "or set IMAPSYNC_BIN, then restart the service."
        }), 503

    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"error": "No CSV file uploaded."}), 400

    rows, row_errors = bulk_csv.parse_bulk_csv(request.files["file"].read())
    if not rows:
        return jsonify({
            "error": "No valid rows found in the CSV.", "row_errors": row_errors,
        }), 400

    name = (request.form.get("name") or "").strip() or None

    try:
        max_concurrent = int(request.form.get("max_concurrent") or 1)
    except ValueError:
        max_concurrent = 1
    max_concurrent = min(max(max_concurrent, 1), MAX_BATCH_CONCURRENCY)

    # "Stage for later": park the batch instead of running it now, either
    # until someone presses Start or until start_at arrives (the scheduler
    # loop picks it up). A staged batch necessarily stores its rows'
    # passwords encrypted — there is nowhere else for them to live while it
    # waits, since the uploaded CSV is never written to disk — so staging
    # implies the same "keep resumable" handling a running batch opts into,
    # and the UI says so.
    stage = (request.form.get("stage") or "").lower() in ("1", "true", "yes", "on")
    start_at = None
    if stage and request.form.get("start_at"):
        try:
            start_at = float(request.form["start_at"])
        except ValueError:
            return jsonify({"error": "Invalid start time."}), 400

    # Assigned up front (rather than lazily inside _run_batch_thread) so
    # scheduled-delta-sync credentials, stored synchronously below, are
    # keyed to the exact same ids each row's job will use once the
    # background thread actually creates it.
    for row in rows:
        row["_job_id"] = str(uuid.uuid4())

    batch_id = str(uuid.uuid4())
    db.create_batch(
        DB_PATH, batch_id, len(rows), time.time(), name=name, max_concurrent=max_concurrent,
        status="staged" if stage else "running", start_at=start_at,
    )
    with BATCHES_LOCK:
        BATCHES[batch_id] = {"lines": [], "subscribers": [], "done": False}

    # Every row gets a DB row up front, status "queued" (host/user/options
    # only — never a password), before any of them actually run. Otherwise a
    # row still waiting its turn (likely for most of a large batch, e.g. at
    # max_concurrent=1) has no record anywhere if the server dies before its
    # turn comes — the uploaded CSV itself is never saved to disk, so that
    # row's host/user would simply be gone, forcing a full CSV re-upload
    # instead of just "Download failed CSV" picking it up like any other
    # interrupted row (see mark_orphaned_running_as_interrupted).
    # A staged batch's rows get their own status rather than "queued", so
    # the startup orphan sweep (which relabels every running/queued row
    # "interrupted", since nothing can still be running after a restart)
    # leaves them alone — a staged batch is *supposed* to survive a restart
    # untouched and still be waiting afterwards.
    for row in rows:
        db.create_job(
            DB_PATH, _new_job_dict(row, job_id=row["_job_id"]),
            batch_id=batch_id, status="staged" if stage else "queued",
        )

    # Opt-in: keep every row's password encrypted in the vault (same
    # mechanism as delta sync below) until this batch finishes, so the
    # startup auto-resume sweep (_auto_resume_interrupted_batches) can pick
    # up right where a crash left off without anyone retyping passwords —
    # for a batch of hundreds/thousands of rows that's the difference
    # between an automatic recovery and a very tedious afternoon. Purged the
    # moment the batch finishes either way — see the end of
    # _run_batch_thread.
    keep_passwords = (request.form.get("keep_passwords") or "").lower() in ("1", "true", "yes", "on")
    if keep_passwords or stage:
        now = time.time()
        for row in rows:
            db.store_credentials(
                DB_PATH, row["_job_id"],
                crypto_store.encrypt(row["password1"]), crypto_store.encrypt(row["password2"]), now,
            )

    schedule_enabled = (request.form.get("schedule_enabled") or "").lower() in ("1", "true", "yes", "on")
    if schedule_enabled:
        interval_hours = float(request.form.get("schedule_interval_hours") or 0)
        if interval_hours > 0:
            pairs = [(row["_job_id"], row["password1"], row["password2"]) for row in rows]
            _enable_delta_sync("batch", batch_id, interval_hours, pairs)

    if stage:
        return jsonify({
            "batch_id": batch_id, "total": len(rows), "row_errors": row_errors,
            "staged": True, "start_at": start_at,
        })

    threading.Thread(
        target=_run_batch_thread, args=(batch_id, rows), kwargs={"max_concurrent": max_concurrent}, daemon=True
    ).start()

    return jsonify({"batch_id": batch_id, "total": len(rows), "row_errors": row_errors})


@app.route("/api/bulk/<batch_id>/stop", methods=["POST"])
def bulk_stop(batch_id):
    batch = db.get_batch(DB_PATH, batch_id)
    if not batch:
        return jsonify({"error": "Unknown batch id."}), 404
    if batch["status"] != "running":
        return jsonify({"error": "This batch isn't running any more."}), 400
    with BATCH_CANCEL_LOCK:
        BATCH_CANCEL_REQUESTS.add(batch_id)
    # Also kill whichever row is actively running right now, instead of
    # only blocking rows that haven't started yet — otherwise Stop does
    # nothing visible for a batch with just one row (or when concurrency
    # means every row is already in flight).
    for job in db.list_jobs_by_batch(DB_PATH, batch_id):
        if job["status"] == "running":
            _kill_running_job(job["id"])
    return jsonify({"ok": True})


@app.route("/api/batches/<batch_id>/jobs")
def batch_jobs(batch_id):
    jobs = db.list_jobs_by_batch(DB_PATH, batch_id)
    # Never the password itself — just whether one is sitting encrypted in
    # the vault for this row (see batch_retry), so the Retry-rows modal can
    # let those rows through without retyping it.
    for job in jobs:
        job["has_stored_password"] = db.get_credentials(DB_PATH, job["id"]) is not None
    return jsonify(jobs)


@app.route("/api/batches/<batch_id>/failed.csv")
def batch_failed_csv(batch_id):
    jobs = [j for j in db.list_jobs_by_batch(DB_PATH, batch_id) if j["status"] in ("error", "interrupted")]
    return Response(
        bulk_csv.build_rows_csv(jobs), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=imapsync-retry-{batch_id[:8]}.csv"},
    )


@app.route("/api/batches/<batch_id>/retry", methods=["POST"])
def batch_retry(batch_id):
    """
    Re-runs a chosen subset of one batch's failed/interrupted rows IN
    PLACE — same batch, same rows (see _retry_batch_rows) — the in-browser
    alternative to "Download failed CSV" for anyone who'd rather not have
    passwords pass through a CSV file at all. Each row's host/port/SSL/
    username/options are pulled back out of that row's own DB record
    (never trusted from the client); only the passwords come from the
    request. Not stored unless "keep_passwords" is set, in which case
    they're kept only until this batch next finishes — see
    _retry_batch_rows.
    """
    if not IMAPSYNC_BIN:
        return jsonify({
            "error": "imapsync binary not found on this server. Run install.sh, "
                     "or set IMAPSYNC_BIN, then restart the service."
        }), 503

    batch = db.get_batch(DB_PATH, batch_id)
    if not batch:
        return jsonify({"error": "Unknown batch id."}), 404

    entries = (request.get_json(silent=True) or {}).get("rows") or []
    rows, row_errors = [], []
    for entry in entries:
        job_id = entry.get("job_id")
        original = db.get_job(DB_PATH, job_id) if job_id else None
        if not original or original.get("batch_id") != batch_id:
            row_errors.append({"row": job_id or "?", "reason": "Unknown row for this batch."})
            continue
        if original["status"] not in ("error", "interrupted"):
            row_errors.append({"row": job_id, "reason": "Row is not in a retryable state."})
            continue
        password1 = (entry.get("password1") or "").strip()
        password2 = (entry.get("password2") or "").strip()
        if not password1 or not password2:
            # Nothing typed for this row — fall back to a still-stored
            # "auto-resume" credential if this row has one (e.g. it was
            # killed via Stop rather than retyped by hand), instead of
            # making the user retype a password that's already sitting
            # there encrypted for exactly this.
            creds = db.get_credentials(DB_PATH, job_id)
            if creds:
                password1 = password1 or crypto_store.decrypt(creds["enc_password1"])
                password2 = password2 or crypto_store.decrypt(creds["enc_password2"])
        if not password1 or not password2:
            row_errors.append({"row": job_id, "reason": "Missing password(s)."})
            continue
        rows.append({
            "host1": original["host1"], "port1": original["port1"], "ssl1": bool(original["ssl1"]),
            "user1": original["user1"], "authuser1": original.get("authuser1") or None, "password1": password1,
            "host2": original["host2"], "port2": original["port2"], "ssl2": bool(original["ssl2"]),
            "user2": original["user2"], "authuser2": original.get("authuser2") or None, "password2": password2,
            "options": json.loads(original["options_json"] or "{}"),
            "_job_id": job_id,  # reused, not a new id — see _retry_batch_rows
        })

    if not rows:
        return jsonify({"error": "No valid rows to retry.", "row_errors": row_errors}), 400

    keep_passwords = bool((request.get_json(silent=True) or {}).get("keep_passwords"))
    _retry_batch_rows(batch, rows, keep_passwords)

    return jsonify({"batch_id": batch_id, "total": len(rows), "row_errors": row_errors})


@app.route("/api/bulk/stream/<batch_id>")
def bulk_stream(batch_id):
    live_response = _stream_live(BATCHES, BATCHES_LOCK, batch_id)
    if live_response is not None:
        return live_response
    # Batches aren't persisted — once they're done, the individual rows are
    # still all in /api/jobs (filterable by batch_id), but there is no more
    # live control stream to replay.
    return Response(
        _sse("log", {"line": "This batch has finished or its progress view expired. "
                              "Its rows are still in the History tab."}),
        mimetype="text/event-stream",
    )


# ---------------------------------------------------------------------------
# Scheduled delta sync — re-runs a finished job/batch on a timer so newly
# arrived mail keeps getting copied over between the initial migration and
# final cutover. Needs stored credentials (see crypto_store.py); every
# schedule here was created by an explicit opt-in at start time.
# ---------------------------------------------------------------------------

def _run_scheduled_job(sched):
    original = db.get_job(DB_PATH, sched["ref_id"])
    creds = db.get_credentials(DB_PATH, sched["ref_id"])
    if not original or not creds:
        return  # the original job or its stored credentials are gone

    payload = {
        "host1": original["host1"], "port1": original["port1"], "ssl1": bool(original["ssl1"]),
        "user1": original["user1"],
        "host2": original["host2"], "port2": original["port2"], "ssl2": bool(original["ssl2"]),
        "user2": original["user2"],
        "options": json.loads(original["options_json"] or "{}"),
    }
    job = _new_job_dict(payload)
    db.create_job(DB_PATH, job, schedule_id=sched["id"])
    with ACTIVE_LOCK:
        ACTIVE[job["id"]] = {"lines": [], "subscribers": [], "done": False}

    password1 = crypto_store.decrypt(creds["enc_password1"])
    password2 = crypto_store.decrypt(creds["enc_password2"])
    _execute_job(job, password1, password2)


def _run_scheduled_batch(sched):
    original_rows = db.list_jobs_by_batch(DB_PATH, sched["ref_id"])
    rows = []
    for orig in original_rows:
        creds = db.get_credentials(DB_PATH, orig["id"])
        if not creds:
            continue  # this particular row was never enrolled (shouldn't happen, but be safe)
        rows.append({
            "host1": orig["host1"], "port1": orig["port1"], "ssl1": bool(orig["ssl1"]),
            "user1": orig["user1"], "password1": crypto_store.decrypt(creds["enc_password1"]),
            "host2": orig["host2"], "port2": orig["port2"], "ssl2": bool(orig["ssl2"]),
            "user2": orig["user2"], "password2": crypto_store.decrypt(creds["enc_password2"]),
            "options": json.loads(orig["options_json"] or "{}"),
        })
    if not rows:
        return

    original_batch = db.get_batch(DB_PATH, sched["ref_id"]) or {}
    base_name = original_batch.get("name") or f"Batch {sched['ref_id'][:8]}"
    max_concurrent = min(max(int(original_batch.get("max_concurrent") or 1), 1), MAX_BATCH_CONCURRENCY)

    for row in rows:
        row["_job_id"] = str(uuid.uuid4())

    batch_id = str(uuid.uuid4())
    db.create_batch(
        DB_PATH, batch_id, len(rows), time.time(),
        name=f"{base_name} (auto delta sync)", schedule_id=sched["id"], max_concurrent=max_concurrent,
    )
    with BATCHES_LOCK:
        BATCHES[batch_id] = {"lines": [], "subscribers": [], "done": False}

    # Same up-front "queued" row per CSV row as bulk_start — see the comment
    # there for why.
    for row in rows:
        db.create_job(
            DB_PATH, _new_job_dict(row, job_id=row["_job_id"]),
            batch_id=batch_id, schedule_id=sched["id"], status="queued",
        )

    _run_batch_thread(batch_id, rows, schedule_id=sched["id"], max_concurrent=max_concurrent)


def _auto_resume_interrupted_batches():
    """
    Called once at startup, right after mark_orphaned_batches_as_interrupted
    marks any still-"running" batch "interrupted" (the server crashed/
    restarted mid-batch). Any interrupted batch whose rows still have
    "auto-resume" credentials stored (opted into via the "keep passwords
    until this batch finishes" checkbox at bulk_start/Retry-rows time) gets
    its still-pending rows automatically re-run in place (same batch, same
    job ids — see _retry_batch_rows) — no person needs to retype anything,
    nothing new added to History, which is the whole point for a batch
    of hundreds/thousands of rows. A batch that never opted in has nothing
    stored to resume with, and is left exactly as before: retryable by hand
    via History -> Retry rows / Download failed CSV.

    Runs regardless of whether a delta-sync schedule also references this
    batch (kind='batch') — that schedule re-running it on its own next tick
    (which could be hours away) is a *separate* concern from "this batch
    was interrupted and should recover right now"; conflating the two used
    to mean a batch with both checked wouldn't actually auto-resume until
    the schedule's own timer came due. Only the *cleanup* below still
    respects that schedule: it needs the original rows' credentials kept
    indefinitely, so those are left alone instead of purged.
    """
    for batch in db.list_batches_by_status(DB_PATH, "interrupted"):
        pending = [
            j for j in db.list_jobs_by_batch(DB_PATH, batch["id"])
            if j["status"] in ("error", "interrupted")
        ]
        rows = []
        for job in pending:
            creds = db.get_credentials(DB_PATH, job["id"])
            if not creds:
                continue
            rows.append({
                "host1": job["host1"], "port1": job["port1"], "ssl1": bool(job["ssl1"]),
                "user1": job["user1"], "authuser1": job.get("authuser1") or None,
                "password1": crypto_store.decrypt(creds["enc_password1"]),
                "host2": job["host2"], "port2": job["port2"], "ssl2": bool(job["ssl2"]),
                "user2": job["user2"], "authuser2": job.get("authuser2") or None,
                "password2": crypto_store.decrypt(creds["enc_password2"]),
                "options": json.loads(job["options_json"] or "{}"),
                "_job_id": job["id"],  # reused, not a new id — see _retry_batch_rows
            })

        if not rows:
            continue  # nothing stored for this one — leave it for a manual retry

        # keep_passwords=True: if this auto-resumed run also gets
        # interrupted, it can auto-resume again in turn. Credential cleanup
        # (drop the ones that ended up unneeded) happens inside
        # _run_batch_thread once this round actually finishes, same as any
        # other retry — nothing to do here after kicking it off.
        _retry_batch_rows(batch, rows, keep_passwords=True)


def _run_schedule(sched):
    try:
        if sched["kind"] == "job":
            _run_scheduled_job(sched)
        elif sched["kind"] == "batch":
            _run_scheduled_batch(sched)
    finally:
        db.touch_schedule_run(DB_PATH, sched["id"], time.time())


def _start_due_staged_batches():
    """A staged batch given a start time (see bulk_start) is launched from
    here once that time arrives. claim_staged_batch is the compare-and-swap
    that keeps this from racing the Start button — whichever gets there
    first wins, the other sees the batch already running."""
    for batch in db.list_due_staged_batches(DB_PATH, time.time()):
        if db.claim_staged_batch(DB_PATH, batch["id"]):
            _launch_staged_batch(batch)


def _scheduler_loop():
    while True:
        time.sleep(60)
        try:
            due = db.claim_due_schedules(DB_PATH, time.time())
        except Exception:  # noqa: BLE001 — a bad tick must never kill the loop
            due = []
        for sched in due:
            threading.Thread(target=_run_schedule, args=(sched,), daemon=True).start()
        try:
            _start_due_staged_batches()
        except Exception:  # noqa: BLE001 — same: never let one bad batch kill the loop
            pass


@app.route("/api/schedules")
def list_schedules():
    enriched = []
    for sched in db.list_schedules(DB_PATH):
        info = dict(sched)
        if sched["kind"] == "job":
            job = db.get_job(DB_PATH, sched["ref_id"])
            if job:
                info["label"] = f"{job['user1']}@{job['host1']} → {job['user2']}@{job['host2']}"
                # Raw fields too, so the UI can style the account and host
                # differently instead of re-parsing the combined label.
                info["user1"], info["host1"] = job["user1"], job["host1"]
                info["user2"], info["host2"] = job["user2"], job["host2"]
            else:
                info["label"] = "(job deleted)"
        else:
            batch = db.get_batch(DB_PATH, sched["ref_id"])
            info["label"] = (batch.get("name") if batch else None) or f"Batch {sched['ref_id'][:8]}"
        enriched.append(info)
    return jsonify(enriched)


@app.route("/api/schedules/<schedule_id>/run-now", methods=["POST"])
def run_schedule_now(schedule_id):
    sched = db.get_schedule(DB_PATH, schedule_id)
    if not sched:
        return jsonify({"error": "Unknown schedule id."}), 404
    threading.Thread(target=_run_schedule, args=(sched,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/schedules/<schedule_id>/edit", methods=["POST"])
def edit_schedule(schedule_id):
    sched = db.get_schedule(DB_PATH, schedule_id)
    if not sched:
        return jsonify({"error": "Unknown schedule id."}), 404

    payload = request.get_json(silent=True) or {}
    try:
        interval_hours = float(payload.get("interval_hours"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid interval."}), 400
    if interval_hours < 0.25:
        return jsonify({"error": "Minimum interval is 15 minutes (0.25h)."}), 400

    password1 = (payload.get("password1") or "").strip()
    password2 = (payload.get("password2") or "").strip()
    if password1 or password2:
        # A batch schedule covers many accounts with (usually) different
        # passwords, so there's no single field to update here — re-upload
        # the CSV to change a batch's stored credentials instead.
        if sched["kind"] != "job":
            return jsonify({
                "error": "Password can only be edited for a single-migration "
                         "schedule. For a batch, re-upload the CSV to update credentials."
            }), 400
        existing = db.get_credentials(DB_PATH, sched["ref_id"]) or {}
        enc1 = crypto_store.encrypt(password1) if password1 else (existing.get("enc_password1") or crypto_store.encrypt(""))
        enc2 = crypto_store.encrypt(password2) if password2 else (existing.get("enc_password2") or crypto_store.encrypt(""))
        db.store_credentials(DB_PATH, sched["ref_id"], enc1, enc2, time.time())

    # Restart the countdown from now with the new interval, rather than
    # leaving it keyed to the old spacing.
    next_run_at = time.time() + interval_hours * 3600
    db.update_schedule(DB_PATH, schedule_id, interval_hours, next_run_at)
    return jsonify({"ok": True, "next_run_at": next_run_at})


@app.route("/api/schedules/<schedule_id>", methods=["DELETE"])
def delete_schedule(schedule_id):
    sched = db.get_schedule(DB_PATH, schedule_id)
    if not sched:
        return jsonify({"error": "Unknown schedule id."}), 404
    if sched["kind"] == "job":
        db.delete_credentials(DB_PATH, sched["ref_id"])
    else:
        job_ids = [j["id"] for j in db.list_jobs_by_batch(DB_PATH, sched["ref_id"])]
        db.delete_credentials_many(DB_PATH, job_ids)
    db.delete_schedule(DB_PATH, schedule_id)
    return jsonify({"ok": True})


if WON_STARTUP_RACE:
    _auto_resume_interrupted_batches()
threading.Thread(target=_scheduler_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Shared history / log endpoints (used by single AND bulk jobs alike)
# ---------------------------------------------------------------------------

@app.route("/api/jobs")
def list_jobs():
    jobs = db.list_jobs(DB_PATH)
    for job in jobs:
        job["has_stored_password"] = db.get_credentials(DB_PATH, job["id"]) is not None
    return jsonify(jobs)


@app.route("/api/batches")
def list_batches():
    return jsonify(db.list_batches(DB_PATH))


@app.route("/api/test-connection", methods=["POST"])
def test_connection():
    """
    Login-only check for host1/host2, run in parallel — no imapsync process,
    no temp files, nothing written to the DB or the job logs. Used by the
    "Test connection" button on the New migration form before committing to
    a real run.
    """
    payload = request.get_json(silent=True) or {}
    results = {}

    def run_side(side):
        results[side] = conn_test.test_login(
            payload.get(f"host{side}"),
            payload.get(f"port{side}"),
            bool(payload.get(f"ssl{side}")),
            payload.get(f"user{side}"),
            payload.get(f"password{side}"),
            authuser=(payload.get(f"authuser{side}") or "").strip() or None,
        )

    threads = [threading.Thread(target=run_side, args=(side,)) for side in ("1", "2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return jsonify(results)


@app.route("/api/history/clear", methods=["POST"])
def clear_history():
    result = db.clear_history(DB_PATH)
    for log_path in result.pop("log_paths", []):
        try:
            os.remove(log_path)
        except OSError:
            pass
    return jsonify(result)


@app.route("/api/jobs/<job_id>")
def get_job(job_id):
    job = db.get_job(DB_PATH, job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@app.route("/api/jobs/<job_id>/log")
def get_job_log(job_id):
    # Validate against the DB first (same pattern as /api/stream/<job_id>)
    # rather than building a filesystem path straight from the URL segment.
    if not db.get_job(DB_PATH, job_id):
        return jsonify({"error": "not found"}), 404
    log_path = os.path.join(LOGS_DIR, f"{job_id}.log")
    if not os.path.isfile(log_path):
        return Response("(no log)", mimetype="text/plain")
    with open(log_path) as f:
        return Response(f.read(), mimetype="text/plain")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", debug=False, threaded=True, port=port)
