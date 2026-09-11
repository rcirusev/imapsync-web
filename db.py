"""
Tiny SQLite data-access layer for job history.

Deliberately dependency-free (stdlib sqlite3 only) so the install stays a
single `pip install -r requirements.txt` away from working, on any Linux box.
"""

import json
import os
import sqlite3
import threading

_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    host1         TEXT NOT NULL,
    port1         TEXT NOT NULL,
    ssl1          INTEGER NOT NULL,
    user1         TEXT NOT NULL,
    authuser1     TEXT,
    host2         TEXT NOT NULL,
    port2         TEXT NOT NULL,
    ssl2          INTEGER NOT NULL,
    user2         TEXT NOT NULL,
    authuser2     TEXT,
    options_json  TEXT NOT NULL,
    status        TEXT NOT NULL,   -- running | success | error
    folders       INTEGER,
    messages      INTEGER,
    data_mb       REAL,
    errors        INTEGER,
    duration_s    REAL,
    return_code   INTEGER,
    log_path      TEXT,
    error_message TEXT,
    batch_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs (created_at DESC);

CREATE TABLE IF NOT EXISTS batches (
    id             TEXT PRIMARY KEY,
    created_at     REAL NOT NULL,
    total          INTEGER NOT NULL,
    completed      INTEGER NOT NULL DEFAULT 0,
    success        INTEGER NOT NULL DEFAULT 0,
    error          INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL,   -- staged | running | done | interrupted | stopped
    finished_at    REAL,
    name           TEXT,
    max_concurrent INTEGER NOT NULL DEFAULT 1,
    start_at       REAL             -- staged batches only: automatic start time
);
CREATE INDEX IF NOT EXISTS idx_batches_created_at ON batches (created_at DESC);

-- Encrypted passwords — an explicit opt-in exception to "passwords are
-- never stored" (see crypto_store.py), used for exactly two features:
--   1. "Scheduled delta sync" (see schedules below) — kept indefinitely
--      until the schedule is deleted.
--   2. A bulk batch's "auto-resume if the server restarts mid-batch"
--      checkbox — kept only until that batch finishes (success, error, or
--      stopped), then purged (see app.py's _run_batch_thread /
--      _auto_resume_interrupted_batches). Lets a large batch (hundreds of
--      rows) survive a crash and pick back up on its own, without retyping
--      every password, without keeping them around any longer than that.
-- Every other job in this app never has a row here.
CREATE TABLE IF NOT EXISTS credential_vault (
    job_id        TEXT PRIMARY KEY,
    enc_password1 TEXT NOT NULL,
    enc_password2 TEXT NOT NULL,
    created_at    REAL NOT NULL
);

-- One row per recurring delta-sync rule. kind='job' re-runs a single
-- migration (ref_id = that job's id); kind='batch' re-runs a whole bulk
-- CSV batch (ref_id = that batch's id, whose rows supply host/user/options
-- — only the passwords come from credential_vault, keyed by each row's
-- original job_id).
CREATE TABLE IF NOT EXISTS schedules (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,   -- job | batch
    ref_id         TEXT NOT NULL,
    interval_hours REAL NOT NULL,
    created_at     REAL NOT NULL,
    next_run_at    REAL NOT NULL,
    last_run_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_schedules_next_run ON schedules (next_run_at);
"""
# idx_jobs_batch_id is created in init_db() below, AFTER the ALTER TABLE
# upgrade path — not here. On an install whose jobs table pre-dates
# batch_id, "CREATE TABLE IF NOT EXISTS" above is a no-op (the table
# already exists), so an index on batch_id in this same script would fail
# with "no such column: batch_id" before the ALTER TABLE ever runs.


def get_conn(db_path):
    conn = getattr(_LOCAL, "conn", None)
    if conn is None or getattr(_LOCAL, "db_path", None) != db_path:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        _LOCAL.conn = conn
        _LOCAL.db_path = db_path
    return conn


def init_db(db_path):
    conn = get_conn(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    # Upgrade path for installs created before batch_id existed.
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN batch_id TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # Safe now: batch_id is guaranteed to exist either way (fresh CREATE
    # TABLE above, or the ALTER TABLE upgrade just above).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_batch_id ON jobs (batch_id)")
    conn.commit()
    # Same upgrade dance for installs whose batches table pre-dates the
    # optional "name" column.
    try:
        conn.execute("ALTER TABLE batches ADD COLUMN name TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # ...and for schedule_id, which tags jobs/batches spawned by a
    # scheduled delta sync (added along with the schedules table).
    for table in ("jobs", "batches"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN schedule_id TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    # ...and for authuser1/authuser2 (master/admin-account login, distinct
    # from the mailbox being migrated — see conn_test.py / build_command).
    for col in ("authuser1", "authuser2"):
        try:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    # ...and for max_concurrent (bounded worker-pool size for bulk batches —
    # pre-existing installs default every row here to 1, i.e. today's
    # strictly-sequential behavior, since that's what already ran).
    try:
        conn.execute("ALTER TABLE batches ADD COLUMN max_concurrent INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # ...and for start_at: a staged batch's optional automatic start time
    # (epoch seconds). NULL means "waits for someone to press Start".
    try:
        conn.execute("ALTER TABLE batches ADD COLUMN start_at REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


def create_job(db_path, job, batch_id=None, schedule_id=None, status="running"):
    conn = get_conn(db_path)
    conn.execute(
        """INSERT INTO jobs
           (id, created_at, host1, port1, ssl1, user1, authuser1,
            host2, port2, ssl2, user2, authuser2,
            options_json, status, log_path, batch_id, schedule_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job["id"], job["created_at"], job["host1"], job["port1"], int(job["ssl1"]),
            job["user1"], job.get("authuser1") or None,
            job["host2"], job["port2"], int(job["ssl2"]), job["user2"], job.get("authuser2") or None,
            json.dumps(job["options"]), status, job["log_path"], batch_id, schedule_id,
        ),
    )
    conn.commit()


def mark_orphaned_running_as_interrupted(db_path):
    """
    Call once at process startup, right after init_db(). Any row still
    marked 'running' or 'queued' at that point cannot actually be
    running/pending any more — this process just started, so its in-memory
    ACTIVE registry (and every background thread / imapsync child process
    from before) is gone. That happens when the service/VM restarts or
    crashes mid-migration — 'queued' specifically covers a bulk-batch row
    that was written to the DB up front (see bulk_start/_run_scheduled_batch
    in app.py) but whose turn to actually run never came before the crash.

    Re-labels those rows 'interrupted' so History stops showing a phantom
    "Running"/"Queued" job forever. This does NOT lose any progress:
    imapsync itself is incremental, so simply starting a fresh migration
    with the same host/user settings (the History "Resume" button does
    this, or "Download failed CSV" for a whole batch) will skip whatever
    was already copied and only transfer the remainder.
    """
    conn = get_conn(db_path)
    conn.execute(
        """UPDATE jobs SET status = 'interrupted',
               error_message = COALESCE(error_message, ?)
           WHERE status IN ('running', 'queued')""",
        ("Server restarted before this job could finish.",),
    )
    conn.commit()


def create_batch(db_path, batch_id, total, created_at, name=None, schedule_id=None,
                 max_concurrent=1, status="running", start_at=None):
    conn = get_conn(db_path)
    conn.execute(
        """INSERT INTO batches (id, created_at, total, completed, success, error, status, name,
                                schedule_id, max_concurrent, start_at)
           VALUES (?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?)""",
        (batch_id, created_at, total, status, name or None, schedule_id, max_concurrent, start_at),
    )
    conn.commit()


def claim_staged_batch(db_path, batch_id):
    """
    Flips a batch from 'staged' to 'running' and returns True only for the
    caller that actually made that transition. Both the Start button and
    the scheduler's own due-batch sweep go through here, so a batch whose
    automatic start time arrives at the same moment someone presses Start
    can only ever be launched once — the UPDATE's WHERE clause is the
    compare-and-swap, the same trick claim_due_schedules uses.
    """
    conn = get_conn(db_path)
    cur = conn.execute(
        "UPDATE batches SET status = 'running', start_at = NULL WHERE id = ? AND status = 'staged'",
        (batch_id,),
    )
    conn.commit()
    return cur.rowcount == 1


def list_due_staged_batches(db_path, now):
    """Staged batches whose automatic start time has arrived. Claiming each
    one (claim_staged_batch) is a separate step, so this can be read without
    a lock."""
    conn = get_conn(db_path)
    rows = conn.execute(
        """SELECT * FROM batches
           WHERE status = 'staged' AND start_at IS NOT NULL AND start_at <= ?
           ORDER BY start_at""",
        (now,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_batch(db_path, batch_id):
    """Removes a batch outright, along with its job rows, their logs' DB
    records and any stored credentials. Only meant for a batch that never
    ran (staged) — a batch with real results belongs in history, where
    clear_history decides its fate instead. Returns the deleted jobs'
    log paths so the caller can delete the files."""
    conn = get_conn(db_path)
    jobs = conn.execute("SELECT id, log_path FROM jobs WHERE batch_id = ?", (batch_id,)).fetchall()
    job_ids = [j["id"] for j in jobs]
    if job_ids:
        marks = ",".join("?" * len(job_ids))
        conn.execute(f"DELETE FROM credential_vault WHERE job_id IN ({marks})", job_ids)
        conn.execute(f"DELETE FROM jobs WHERE id IN ({marks})", job_ids)
    conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    conn.commit()
    return [j["log_path"] for j in jobs if j["log_path"]]


def stop_batch(db_path, batch_id, finished_at):
    """Marks a batch as deliberately stopped by the user (via the Stop
    button), as opposed to 'interrupted' (the server died) or 'done' (ran to
    completion). The row that was in flight when Stop was requested is left
    to finish normally — only rows that hadn't started yet are skipped."""
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE batches SET status = 'stopped', finished_at = ? WHERE id = ?",
        (finished_at, batch_id),
    )
    conn.commit()


def finish_batch(db_path, batch_id, finished_at):
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE batches SET status = 'done', finished_at = ? WHERE id = ?",
        (finished_at, batch_id),
    )
    conn.commit()


def mark_orphaned_batches_as_interrupted(db_path):
    """Same idea as mark_orphaned_running_as_interrupted, but for whole bulk
    batches: a batch still 'running' at process startup means the server
    died partway through it. Rows already completed by then each have their
    own 'interrupted'/finished job row (handled separately); whatever rows
    hadn't started yet were never persisted anywhere (CSV rows only ever
    live in memory, by design — see bulk_csv.py), so the safest way to
    finish the job is to re-upload the same CSV again: imapsync is
    incremental, so already-migrated mailboxes just finish instantly."""
    conn = get_conn(db_path)
    conn.execute("UPDATE batches SET status = 'interrupted' WHERE status = 'running'")
    conn.commit()


def list_batches(db_path, limit=50):
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_batch(db_path, batch_id):
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    return dict(row) if row else None


def list_batches_by_status(db_path, status):
    """Unlike list_batches, no limit — used by the startup auto-resume sweep,
    which must not silently miss an old interrupted batch just because more
    than 50 batches have run since."""
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM batches WHERE status = ? ORDER BY created_at DESC", (status,)
    ).fetchall()
    return [dict(r) for r in rows]


def reset_job_for_retry(db_path, job_id):
    """Resets an existing job row back to 'queued' with its stats cleared,
    for a retry that reuses the same row (same id, so its log file and any
    "auto-resume" credential stay associated with it) instead of creating
    a brand new one — keeps History to one row per mailbox across repeated
    retries instead of growing with every attempt."""
    conn = get_conn(db_path)
    conn.execute(
        """UPDATE jobs SET status = 'queued', started_at = NULL, finished_at = NULL,
               folders = NULL, messages = NULL, data_mb = NULL, errors = NULL,
               duration_s = NULL, return_code = NULL, error_message = NULL
           WHERE id = ?""",
        (job_id,),
    )
    conn.commit()


def reopen_batch_for_retry(db_path, batch_id):
    """Sets an existing batch row back to 'running' for a retry that
    reuses it in place instead of creating a new batch."""
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE batches SET status = 'running', finished_at = NULL WHERE id = ?",
        (batch_id,),
    )
    conn.commit()


def recompute_batch_progress(db_path, batch_id):
    """Recomputes and persists a batch's completed/success/error counts
    from its rows' actual current statuses, rather than tracking them
    incrementally — so a retry that reuses existing rows in place (see
    reset_job_for_retry) stays correct without the caller needing to know
    which rows are "new" this round vs. already-settled from a previous
    one. Returns (completed, success, error)."""
    conn = get_conn(db_path)
    rows = conn.execute("SELECT status FROM jobs WHERE batch_id = ?", (batch_id,)).fetchall()
    completed = sum(1 for r in rows if r["status"] in ("success", "error", "interrupted"))
    success = sum(1 for r in rows if r["status"] == "success")
    error = completed - success
    conn.execute(
        "UPDATE batches SET completed = ?, success = ?, error = ? WHERE id = ?",
        (completed, success, error, batch_id),
    )
    conn.commit()
    return completed, success, error


def mark_started(db_path, job_id, started_at):
    # Also flips status to 'running' — a bulk-batch row is pre-created as
    # 'queued' (see create_job's status param) before its worker actually
    # gets to it, so this is what promotes it out of that state. A no-op
    # status-wise for a single migration, which is already 'running' by
    # the time this is called.
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
        (started_at, job_id),
    )
    conn.commit()


def finish_job(db_path, job_id, *, status, finished_at, folders, messages, data_mb,
                errors, duration_s, return_code, error_message=None):
    conn = get_conn(db_path)
    conn.execute(
        """UPDATE jobs SET status=?, finished_at=?, folders=?, messages=?, data_mb=?,
           errors=?, duration_s=?, return_code=?, error_message=? WHERE id=?""",
        (status, finished_at, folders, messages, data_mb, errors, duration_s,
         return_code, error_message, job_id),
    )
    conn.commit()


def list_jobs(db_path, limit=100):
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_job(db_path, job_id):
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs_by_batch(db_path, batch_id):
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM jobs WHERE batch_id = ? ORDER BY created_at ASC", (batch_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def clear_history(db_path):
    """
    Deletes finished single migrations and bulk batches from history.

    Never touches anything currently running or still staged (uploaded but
    not started yet — that is work waiting to happen, not history), and
    never touches a job/batch that an active delta-sync schedule still
    needs to re-run (a job-kind schedule's own job, or every job belonging
    to a batch-kind schedule's batch) — deleting those out from under an
    active schedule would make its next automatic run silently find nothing
    and do nothing.

    Returns {"jobs_deleted": n, "batches_deleted": n, "log_paths": [...]}
    — the caller is responsible for removing the log files at log_paths.
    """
    conn = get_conn(db_path)

    schedules = conn.execute("SELECT kind, ref_id FROM schedules").fetchall()
    protected_batch_ids = {s["ref_id"] for s in schedules if s["kind"] == "batch"}
    protected_job_ids = {s["ref_id"] for s in schedules if s["kind"] == "job"}

    all_batches = conn.execute("SELECT id, status FROM batches").fetchall()
    active_batch_ids = {b["id"] for b in all_batches if b["status"] in ("running", "staged")}
    keep_batch_ids = protected_batch_ids | active_batch_ids

    all_jobs = conn.execute("SELECT id, batch_id, status, log_path FROM jobs").fetchall()
    jobs_to_delete = [
        j for j in all_jobs
        if j["status"] not in ("running", "staged")
        and j["id"] not in protected_job_ids
        and (not j["batch_id"] or j["batch_id"] not in keep_batch_ids)
    ]
    batches_to_delete = [
        b for b in all_batches
        if b["status"] not in ("running", "staged") and b["id"] not in protected_batch_ids
    ]

    job_ids = [j["id"] for j in jobs_to_delete]
    batch_ids = [b["id"] for b in batches_to_delete]

    if job_ids:
        conn.executemany("DELETE FROM jobs WHERE id = ?", [(jid,) for jid in job_ids])
        # A deleted job's "auto-resume if the server restarts mid-batch"
        # credentials (if any) would otherwise become orphaned — still
        # sitting encrypted in the vault with no job left to ever clean
        # them up. protected_job_ids above already keeps job_ids from
        # including anything an active schedule still needs, so this is
        # safe to do unconditionally for every job actually being deleted.
        conn.executemany("DELETE FROM credential_vault WHERE job_id = ?", [(jid,) for jid in job_ids])
    if batch_ids:
        conn.executemany("DELETE FROM batches WHERE id = ?", [(bid,) for bid in batch_ids])
    conn.commit()

    return {
        "jobs_deleted": len(job_ids),
        "batches_deleted": len(batch_ids),
        "log_paths": [j["log_path"] for j in jobs_to_delete if j["log_path"]],
    }


# ---------------------------------------------------------------------------
# Credential vault + scheduled delta sync — see crypto_store.py for why this
# exists as an explicit opt-in exception to "passwords are never stored".
# ---------------------------------------------------------------------------

def store_credentials(db_path, job_id, enc_password1, enc_password2, created_at):
    conn = get_conn(db_path)
    conn.execute(
        """INSERT INTO credential_vault (job_id, enc_password1, enc_password2, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET
               enc_password1 = excluded.enc_password1,
               enc_password2 = excluded.enc_password2""",
        (job_id, enc_password1, enc_password2, created_at),
    )
    conn.commit()


def get_credentials(db_path, job_id):
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM credential_vault WHERE job_id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def delete_credentials(db_path, job_id):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM credential_vault WHERE job_id = ?", (job_id,))
    conn.commit()


def delete_credentials_many(db_path, job_ids):
    if not job_ids:
        return
    conn = get_conn(db_path)
    conn.executemany("DELETE FROM credential_vault WHERE job_id = ?", [(j,) for j in job_ids])
    conn.commit()


def create_schedule(db_path, schedule_id, kind, ref_id, interval_hours, created_at):
    conn = get_conn(db_path)
    conn.execute(
        """INSERT INTO schedules (id, kind, ref_id, interval_hours, created_at, next_run_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (schedule_id, kind, ref_id, interval_hours, created_at, created_at + interval_hours * 3600),
    )
    conn.commit()


def list_schedules(db_path):
    conn = get_conn(db_path)
    rows = conn.execute("SELECT * FROM schedules ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_schedule(db_path, schedule_id):
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return dict(row) if row else None


def get_schedule_by_ref(db_path, kind, ref_id):
    """Used to tell whether a given job/batch already has an active
    schedule pointing at it — e.g. so end-of-batch credential cleanup
    doesn't purge passwords a delta-sync schedule still needs indefinitely."""
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM schedules WHERE kind = ? AND ref_id = ?", (kind, ref_id)
    ).fetchone()
    return dict(row) if row else None


def delete_schedule(db_path, schedule_id):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    conn.commit()


def touch_schedule_run(db_path, schedule_id, last_run_at):
    conn = get_conn(db_path)
    conn.execute("UPDATE schedules SET last_run_at = ? WHERE id = ?", (last_run_at, schedule_id))
    conn.commit()


def update_schedule(db_path, schedule_id, interval_hours, next_run_at):
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE schedules SET interval_hours = ?, next_run_at = ? WHERE id = ?",
        (interval_hours, next_run_at, schedule_id),
    )
    conn.commit()


def reschedule(db_path, schedule_id, next_run_at):
    conn = get_conn(db_path)
    conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (next_run_at, schedule_id))
    conn.commit()


def claim_due_schedules(db_path, now):
    """
    Returns every schedule whose next_run_at has passed, and atomically
    advances each one's next_run_at by its own interval as it's claimed.

    The advance uses UPDATE ... WHERE next_run_at <= ? (a compare-and-swap,
    not a separate SELECT-then-UPDATE) specifically so this stays correct
    if gunicorn is running more than one worker process (install.sh's
    default is 2): SQLite serializes writers across processes, so at most
    one worker's UPDATE can ever match a given row before the next_run_at
    it just wrote moves the row out of range for the others — without this,
    two workers polling at once could each claim the same due schedule and
    run it twice.
    """
    conn = get_conn(db_path)
    rows = conn.execute("SELECT * FROM schedules WHERE next_run_at <= ?", (now,)).fetchall()
    claimed = []
    for row in rows:
        sched = dict(row)
        next_run_at = now + sched["interval_hours"] * 3600
        cur = conn.execute(
            "UPDATE schedules SET next_run_at = ? WHERE id = ? AND next_run_at <= ?",
            (next_run_at, sched["id"], now),
        )
        conn.commit()
        if cur.rowcount == 1:
            sched["next_run_at"] = next_run_at
            claimed.append(sched)
    return claimed
