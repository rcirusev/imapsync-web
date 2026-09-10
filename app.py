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

import hmac
import json
import os
import queue
import threading
import time
import uuid

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
# starting each new row — the row already in flight is left to finish
# normally, only rows that hadn't started yet are skipped.
BATCH_CANCEL_REQUESTS = set()
BATCH_CANCEL_LOCK = threading.Lock()


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
    the final status ("success" | "error"). Used by both a lone migration
    and each row of a bulk batch.
    """
    job_id = job["id"]
    db.mark_started(DB_PATH, job_id, time.time())

    def on_line(line):
        _broadcast(ACTIVE, ACTIVE_LOCK, job_id, "log", {"line": line})

    try:
        with open(job["log_path"], "w") as log_fp:
            returncode, full_text, elapsed = runner.run_imapsync(
                IMAPSYNC_BIN, job, password1, password2, log_fp, on_line
            )
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

def _run_batch_thread(batch_id, rows, schedule_id=None):
    total = len(rows)
    success = errors = 0
    stopped_early = False

    for index, row in enumerate(rows, start=1):
        with BATCH_CANCEL_LOCK:
            if batch_id in BATCH_CANCEL_REQUESTS:
                BATCH_CANCEL_REQUESTS.discard(batch_id)
                stopped_early = True
        if stopped_early:
            break  # a Stop request landed while the previous row was running; skip the rest

        job = _new_job_dict(row, job_id=row.get("_job_id"))
        db.create_job(DB_PATH, job, batch_id=batch_id, schedule_id=schedule_id)
        with ACTIVE_LOCK:
            ACTIVE[job["id"]] = {"lines": [], "subscribers": [], "done": False}

        _broadcast(BATCHES, BATCHES_LOCK, batch_id, "row_start", {
            "index": index, "total": total, "job_id": job["id"],
            "host1": job["host1"], "user1": job["user1"],
            "host2": job["host2"], "user2": job["user2"],
        })

        status = _execute_job(job, row["password1"], row["password2"])
        if status == "success":
            success += 1
        else:
            errors += 1
        # Persisted after every row (not just at the end) so the batch's
        # progress survives a page reload, a dropped connection, or even
        # this process dying partway through — History always shows how
        # far a bulk run actually got, not just whether it finished.
        db.update_batch_progress(DB_PATH, batch_id, index, success, errors)

        finished = db.get_job(DB_PATH, job["id"])
        _broadcast(BATCHES, BATCHES_LOCK, batch_id, "row_done", {
            "index": index, "total": total, "job_id": job["id"],
            "host1": job["host1"], "user1": job["user1"],
            "host2": job["host2"], "user2": job["user2"],
            "status": finished["status"], "messages": finished["messages"],
            "errors": finished["errors"], "duration_s": finished["duration_s"],
        })

    if stopped_early:
        db.stop_batch(DB_PATH, batch_id, time.time())
    else:
        db.finish_batch(DB_PATH, batch_id, time.time())

    _broadcast(BATCHES, BATCHES_LOCK, batch_id, "batch_done", {
        "total": total, "success": success, "error": errors, "stopped": stopped_early,
    })
    _close(BATCHES, BATCHES_LOCK, batch_id)


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

    # Assigned up front (rather than lazily inside _run_batch_thread) so
    # scheduled-delta-sync credentials, stored synchronously below, are
    # keyed to the exact same ids each row's job will use once the
    # background thread actually creates it.
    for row in rows:
        row["_job_id"] = str(uuid.uuid4())

    batch_id = str(uuid.uuid4())
    db.create_batch(DB_PATH, batch_id, len(rows), time.time(), name=name)
    with BATCHES_LOCK:
        BATCHES[batch_id] = {"lines": [], "subscribers": [], "done": False}

    schedule_enabled = (request.form.get("schedule_enabled") or "").lower() in ("1", "true", "yes", "on")
    if schedule_enabled:
        interval_hours = float(request.form.get("schedule_interval_hours") or 0)
        if interval_hours > 0:
            pairs = [(row["_job_id"], row["password1"], row["password2"]) for row in rows]
            _enable_delta_sync("batch", batch_id, interval_hours, pairs)

    threading.Thread(target=_run_batch_thread, args=(batch_id, rows), daemon=True).start()

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
    return jsonify({"ok": True})


@app.route("/api/batches/<batch_id>/jobs")
def batch_jobs(batch_id):
    return jsonify(db.list_jobs_by_batch(DB_PATH, batch_id))


@app.route("/api/batches/<batch_id>/failed.csv")
def batch_failed_csv(batch_id):
    jobs = [j for j in db.list_jobs_by_batch(DB_PATH, batch_id) if j["status"] in ("error", "interrupted")]
    return Response(
        bulk_csv.build_rows_csv(jobs), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=imapsync-retry-{batch_id[:8]}.csv"},
    )


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

    for row in rows:
        row["_job_id"] = str(uuid.uuid4())

    batch_id = str(uuid.uuid4())
    db.create_batch(
        DB_PATH, batch_id, len(rows), time.time(),
        name=f"{base_name} (auto delta sync)", schedule_id=sched["id"],
    )
    with BATCHES_LOCK:
        BATCHES[batch_id] = {"lines": [], "subscribers": [], "done": False}
    _run_batch_thread(batch_id, rows, schedule_id=sched["id"])


def _run_schedule(sched):
    try:
        if sched["kind"] == "job":
            _run_scheduled_job(sched)
        elif sched["kind"] == "batch":
            _run_scheduled_batch(sched)
    finally:
        db.touch_schedule_run(DB_PATH, sched["id"], time.time())


def _scheduler_loop():
    while True:
        time.sleep(60)
        try:
            due = db.claim_due_schedules(DB_PATH, time.time())
        except Exception:  # noqa: BLE001 — a bad tick must never kill the loop
            due = []
        for sched in due:
            threading.Thread(target=_run_schedule, args=(sched,), daemon=True).start()


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


threading.Thread(target=_scheduler_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Shared history / log endpoints (used by single AND bulk jobs alike)
# ---------------------------------------------------------------------------

@app.route("/api/jobs")
def list_jobs():
    return jsonify(db.list_jobs(DB_PATH))


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
