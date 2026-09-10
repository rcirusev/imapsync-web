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
bulk rows: marks it started in the DB, streams `imapsync`'s output into the
`ACTIVE` in-memory registry (keyed by job id) which `/api/stream/<job_id>`
tails via SSE, persists the parsed result, and closes the stream. Jobs run
as daemon background threads with no concurrency cap.

A **batch** (`BATCHES` registry, keyed by batch id) runs its rows
sequentially via `_run_batch_thread`, broadcasting `row_start`/`row_done`/
`batch_done` control events; the actual per-row log still flows through the
matching `ACTIVE[job_id]` stream, not through the batch stream.

On process startup, any job/batch still `running` in the DB is relabeled
`interrupted` (`db.mark_orphaned_running_as_interrupted` /
`mark_orphaned_batches_as_interrupted`) — the in-memory `ACTIVE`/`BATCHES`
registries don't survive a restart, so a "Running" row at startup is always
stale. Resuming relies on `imapsync` itself being incremental (re-running the
same host/user/options only copies what's missing), not on any
checkpoint/resume logic in this app.

### Scheduled delta sync

The only feature that stores a password. `_enable_delta_sync` encrypts and
stores credentials in `credential_vault` (keyed by job id) plus a row in
`schedules` (kind `job` or `batch`, `ref_id` pointing at the job/batch to
re-run). A background loop (`_scheduler_loop`, 60s tick) claims due schedules
via `db.claim_due_schedules`, which uses a compare-and-swap `UPDATE ...
WHERE next_run_at <= ?` specifically so multiple gunicorn worker processes
polling the same SQLite DB can't double-claim and double-run a schedule.

### Security-relevant conventions (don't casually change)

- **Passwords are never persisted except in the credential vault above.**
  They're written to `0600` temp files for imapsync's `--passfile1/2` flags
  (never on the command line, never in logs/DB) and deleted immediately
  after the process exits (`imapsync_runner.run_imapsync`).
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
