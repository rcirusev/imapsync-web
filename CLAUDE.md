# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hosted Flask web UI that wraps the real `imapsync` binary: runs actual
migrations (single form or bulk CSV), streams live stdout to the browser over
SSE, and keeps history/scheduling in a local SQLite DB. No JS framework, no
build step, no test suite — plain Flask + vanilla JS + Jinja templates.

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000, Flask dev server
```

Real migrations require the `imapsync` binary on `PATH` (or `IMAPSYNC_BIN` set)
— without it the app still runs but shows an "imapsync not found" banner and
refuses to start jobs. There is no mocking layer for it.

No test suite, linter, or CI check exists for the Python/JS code — the only
CI jobs are `.github/workflows/check-upstream.yml` (daily, bumps
`IMAPSYNC_UPSTREAM_SHA.txt` when upstream imapsync moves) and
`docker-publish.yml` (builds/pushes the image to GHCR on push to `main`).
Verify changes by running the app locally and exercising the UI.

Docker: `docker build -t imapsync-web .` / `docker compose up -d`. The
Dockerfile builds imapsync from source in a separate stage (Ubuntu 24.04,
same package list as `install.sh`'s apt fallback) — keep the two package
lists in sync if either changes.

## Architecture

Six flat Python modules at the repo root, each with one job:

- **`app.py`** — the Flask app: routes, SSE plumbing, in-memory job/batch
  registries, the scheduler loop. This is the orchestrator; it calls into
  every other module rather than containing migration/DB/crypto logic itself.
- **`imapsync_runner.py`** — turns a job dict into a real `imapsync` command
  line (`build_command`), runs it (`run_imapsync`), and best-effort-parses its
  stdout for summary stats (`parse_summary`). This is where new imapsync CLI
  flags get added as form options.
- **`bulk_csv.py`** — CSV parsing/validation for the Bulk tab and the
  downloadable template/retry-CSV generators. Uploaded CSVs are parsed
  in-memory only, never written to disk.
- **`db.py`** — dependency-free `sqlite3` data-access layer (jobs, batches,
  schedules, credential vault). No ORM; schema lives in one `SCHEMA` string
  plus small `ALTER TABLE` upgrade blocks in `init_db()` for columns added
  after the original release — follow that pattern (try `ALTER TABLE`, catch
  `OperationalError`) when adding a new column, rather than editing `SCHEMA`
  in a way that breaks existing installs.
- **`crypto_store.py`** — Fernet encryption for the one feature that persists
  a password: scheduled delta sync. Key lives at `<data dir>/secret.key`
  (0600, created on first use).
- **`conn_test.py`** — standalone IMAP `LOGIN`/SASL-PLAIN check via `imaplib`
  for the "Test connection" button; deliberately independent of imapsync.

Frontend is `templates/index.html` (New migration / Bulk CSV / History tabs)
+ `static/js/app.js` (SSE consumption, form handling, bulk upload, history
polling, log viewer) + `static/css/style.css`. No bundler — `app.py` appends
each static file's mtime as a cache-busting query string
(`_asset_version`/`asset_version`), so edits to `app.js`/`style.css` are
picked up on refresh without a build step.

### Job execution model

A migration (single or one bulk-CSV row) becomes a **job dict**
(`_new_job_dict`) with a generated `id`. `_execute_job` is the single
codepath that runs one job to completion, for both single migrations and
bulk rows: marks it started in the DB (`db.mark_started`, which also flips
status to `running`), streams `imapsync`'s output into the `ACTIVE`
in-memory registry (keyed by job id) which `/api/stream/<job_id>` tails via
SSE, persists the parsed result, and closes the stream. Jobs run as daemon
background threads.

A **batch** (`BATCHES` registry, keyed by batch id) runs its rows through a
bounded `ThreadPoolExecutor` (`_run_batch_thread`, `max_concurrent` workers,
default 1 = fully sequential), broadcasting `row_start`/`row_done`/
`batch_done` control events; the actual per-row log still flows through the
matching `ACTIVE[job_id]` stream, not through the batch stream. Every row
gets its DB job row created **up front**, status `queued` (host/user/
options only, never a password) — before any worker actually picks it up —
specifically so a row still waiting its turn has a durable record if the
process dies before its turn comes; `db.mark_started` promotes it to
`running` when a worker finally gets to it. A row skipped by a Stop request
is written straight to `interrupted` instead of vanishing.

On process startup, any job/batch still `running` or `queued` in the DB is
relabeled `interrupted` (`db.mark_orphaned_running_as_interrupted` /
`mark_orphaned_batches_as_interrupted`) — the in-memory `ACTIVE`/`BATCHES`
registries don't survive a restart, so a "Running"/"Queued" row at startup
is always stale. **This whole sweep (plus `_auto_resume_interrupted_batches`
below) is gated by `WON_STARTUP_RACE`** — an advisory, non-blocking
`fcntl.flock` on `<data dir>/.startup.lock` acquired at import time — so
that only ONE gunicorn worker process ever runs it per boot, not each of
`install.sh`/the Dockerfile's `--workers 2` independently. Without this, two
workers booting at once could both see the same interrupted batch and each
launch their own resume of it, or one worker's brand-new resumed batch
(rows briefly `queued` the instant they're created) could get caught by a
*sibling* worker's own orphan sweep — which has no way to tell "genuinely
stale from before this boot" apart from "a sibling worker just created
this" — cascading into repeated `(auto-resumed) (auto-resumed)` batches.
`_scheduler_loop`'s 60s poll deliberately runs unconditionally in every
worker instead, since it's already safe to run concurrently (see its own
CAS-based `db.claim_due_schedules`, below) — don't apply the same
`WON_STARTUP_RACE` gating there.

Recovering an interrupted batch's rows relies on
`imapsync` itself being incremental (re-running the same host/user/options
only copies what's missing), not on any checkpoint/resume logic in this app
— "Resume" (single job), "Retry rows"/"Download failed CSV" (a batch), and
auto-resume (below) are all just convenient ways to re-supply
host/user/options (+ a password) for another `_new_job_dict` / `_execute_job`
run, nothing more.

Retrying a batch's failed/interrupted rows — by hand via `/api/batches/
<id>/retry` (the "Retry rows" modal) or automatically via
`_auto_resume_interrupted_batches` (below) — goes through the shared
`_launch_retry_batch` helper, which is `bulk_start`'s batch-creation tail
(create batch → queued rows → background thread) factored out for reuse
against rows built from *already-stored* job records instead of a freshly
parsed CSV.

### Scheduled delta sync

`_enable_delta_sync` encrypts and stores credentials in `credential_vault`
(keyed by job id) plus a row in `schedules` (kind `job` or `batch`, `ref_id`
pointing at the job/batch to re-run) — kept **indefinitely** until the
schedule is explicitly deleted. A background loop (`_scheduler_loop`, 60s
tick) claims due schedules via `db.claim_due_schedules`, which uses a
compare-and-swap `UPDATE ... WHERE next_run_at <= ?` specifically so
multiple gunicorn worker processes polling the same SQLite DB can't
double-claim and double-run a schedule.

### Bulk batch auto-resume ("keep passwords until this batch finishes")

The other `credential_vault` use, and the only one that isn't indefinite:
opting in via the Bulk tab's or Retry-rows modal's "Auto-resume if the
server restarts mid-batch" checkbox stores that batch's row passwords
alongside its `queued` rows, purged the moment the batch reaches a terminal
state (`_run_batch_thread`'s end, unless `db.get_schedule_by_ref(...,
"batch", batch_id)` finds an active delta-sync schedule for that exact
batch_id — that schedule owns the indefinite copy instead). On startup,
right after the orphan-interrupt sweep, `_auto_resume_interrupted_batches`
scans for `interrupted` batches with still-stored credentials and silently
relaunches their pending rows via `_launch_retry_batch` — no user action.
`db.clear_history` also purges any `credential_vault` row belonging to a
job it deletes, so a manual history-clear can't orphan one.

### Security-relevant conventions (don't casually change)

- **Passwords are never persisted except in `credential_vault`, and only
  for the two explicit opt-ins above** (scheduled delta sync; a batch's
  auto-resume checkbox, purged once that batch finishes). Every other
  password is written to a `0600` temp file for imapsync's
  `--passfile1/2` flags (never on the command line, never in logs/DB) and
  deleted immediately after the process exits
  (`imapsync_runner.run_imapsync`).
- **CSRF header check** (`app.py::_require_csrf_header`): every non-GET
  request must carry `X-Requested-With: imapsync-web` (sent by
  `static/js/app.js`), since Basic Auth alone doesn't stop cross-site state
  changes. Any new non-GET route relies on this; any new fetch from the
  frontend must set this header.
- **`SERVER_SOFTWARE` is stripped from the imapsync subprocess's env**
  (`imapsync_runner.run_imapsync`) — gunicorn sets it, and imapsync
  misinterprets its presence as "running as CGI," which breaks stdout
  streaming. Don't remove this when touching subprocess launching.
- **CSV formula/DDE injection**: `bulk_csv._csv_safe` prefixes a leading
  apostrophe onto any generated CSV cell that starts with `=+-@`/tab/CR
  before writing it back out (template/retry downloads). Apply the same
  guard to any new CSV-export code path.
- Optional HTTP Basic Auth (`IMAPSYNC_WEB_USERNAME`/`PASSWORD`) is checked
  with `hmac.compare_digest` and only activates when *both* vars are set.

## Extending imapsync options

Form options map to CLI flags in `imapsync_runner.py::build_command` —add
new imapsync flags there, then thread the option through `bulk_csv.py`
(CSV column + `parse_bulk_csv`/`build_template_csv`/`build_rows_csv`),
`templates/index.html` (form field), and `static/js/app.js` (payload
building) to keep the single-migration form and bulk CSV in sync, since they
share the same job-dict shape.
