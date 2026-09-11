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

**Visual identity — deliberately quiet.** This is an admin console for
running real customer migrations, so the styling stays restrained: solid
panels, thin rules, compact spacing, small radii, Manrope for UI text and
DM Mono for machine data (hosts, timestamps, counts, log output). There is
exactly **one accent** (`--series-1`, teal) for primary buttons, focus
rings, the active tab and progress fills, kept separate from the semantic
`--status-good/warning/serious/critical` tokens. Colour and motion have to
encode something — progress, status, liveness; decorative effects
(gradient washes, glass panels, hover lifts, ripples) were explicitly
rejected and shouldn't creep back in. Light is the default and dark is the
same design on a darker ground — `:root` / `:root[data-theme="dark"]` in
`static/css/style.css`; the theme toggle (`#theme-toggle` in app.js)
always stamps an explicit `data-theme`, so the root is never unstamped.
The console keeps its dark surface in both themes, since it is raw process
output.

Both font families are vendored under `static/fonts/` (latin + cyrillic
subsets only) rather than loaded from Google's CDN, so the UI keeps
working — and keeps rendering Cyrillic batch names/hostnames correctly —
in a network-restricted deployment. To change a weight or family:
fetch Google's `css2` endpoint with a browser user-agent, keep only the
`latin`/`cyrillic` `@font-face` blocks, download each distinct `url()`
once into `static/fonts/`, and rewrite the `@font-face` block at the top
of `style.css` to point at the local files. Note that Google serves some
families as a single variable-weight file reused across several
`font-weight` declarations — that is expected, not a duplicate.

### Job execution model

**This app must run as a single gunicorn worker PROCESS** (`--workers 1`,
concurrency comes entirely from `--threads 8` instead — both `install.sh`
and the `Dockerfile` are set up this way; don't "helpfully" bump `--workers`
back up). `ACTIVE`, `BATCHES`, and `BATCH_CANCEL_REQUESTS` below are plain
in-process Python dicts/sets — with more than one worker process, each
would have its own separate copy, and gunicorn does distribute requests
across workers (confirmed empirically: 2 workers under light sequential
load split roughly 85/15, not pinned to one). A live SSE stream or a Stop
click landing on a *different* worker process than the one actually
running that job/batch would silently see/do nothing — `/api/stream`
would fall back to "already done" replay-from-disk, and Stop would set the
cancel flag in a process nobody's checking. Threads within one process
share memory, so this only works correctly as long as there is exactly one
worker process.

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
(not yet started) is written straight to `interrupted` instead of
vanishing; a row already running gets its live `imapsync` subprocess
actually killed (see below), which lands it at `interrupted` the normal
way, through `_execute_job`'s own finish path.

**Stop also kills whatever's actually running**, not just future rows —
`RUNNING_PROCESSES` (job_id -> its live `subprocess.Popen`, set via
`run_imapsync`'s `on_process` callback) and `KILL_REQUESTED` (job_ids
`_execute_job` should report as `interrupted`/"Stopped by user request"
rather than running the exit code through `parse_summary`, which has no
idea a nonzero exit here means "we killed it") make this possible.
`_kill_running_job` (called by both `bulk_stop` and, per-row, anywhere
else that might need it later) signals the process's whole *group*, not
just its PID — `run_imapsync` launches it with `start_new_session=True`
specifically so this reaches any child it may have shelled out to, not
only imapsync itself; skipping that would leave such a child running,
holding the stdout pipe open, and the read loop blocked regardless of
"killing" the direct child. SIGTERM first, SIGKILL after a 5s grace period
if it's still alive. Because imapsync is incremental, killing mid-transfer
is exactly as safe to resume from as a server crash mid-transfer already
is (`_run_batch_thread`'s `stopped_early` — whether the batch itself ends
up `stopped` vs. `done` — considers *either* a pre-start skip or Stop
having been requested at all, since a single-row batch killed mid-flight
never trips the pre-start-skip path on its own).

On process startup, any job/batch still `running` or `queued` in the DB is
relabeled `interrupted` (`db.mark_orphaned_running_as_interrupted` /
`mark_orphaned_batches_as_interrupted`) — the in-memory `ACTIVE`/`BATCHES`
registries don't survive a restart, so a "Running"/"Queued" row at startup
is always stale. **This whole sweep (plus `_auto_resume_interrupted_batches`
below) is gated by `WON_STARTUP_RACE`** — an advisory, non-blocking
`fcntl.flock` on `<data dir>/.startup.lock` acquired at import time — so
that only ONE worker process ever runs it per boot. With `--workers 1`
(above) there is only ever one worker anyway, so this is now defense in
depth rather than the only thing standing between correctness and a
cascade — kept because it's cheap insurance if `--workers` is ever
misconfigured above 1: two workers booting at once could both see the same
interrupted batch and each launch their own resume of it, or one worker's
brand-new resumed batch (rows briefly `queued` the instant they're created)
could get caught by a *sibling* worker's own orphan sweep — which has no
way to tell "genuinely stale from before this boot" apart from "a sibling
worker just created this" — cascading into repeated `(auto-resumed)
(auto-resumed)` batches. `_scheduler_loop`'s 60s poll deliberately runs
unconditionally instead, since it's already safe to run concurrently (see
its own CAS-based `db.claim_due_schedules`, below) even if `--workers` were
misconfigured — don't apply the same `WON_STARTUP_RACE` gating there.

Recovering an interrupted job/batch relies on `imapsync` itself being
incremental (re-running the same host/user/options only copies what's
missing), not on any checkpoint/resume logic in this app — "Resume"
(single job), "Retry rows"/"or CSV" (a batch), and auto-resume (below) are
all just convenient ways to re-supply host/user/options (+ a password) for
another `_execute_job` run, nothing more.

**A batch-row job never gets its own single-job retry UI** — Migration
history and the batch-detail ("View rows") modal both only offer Resume
for a job whose `batch_id` is null; a batch row retries exclusively
through that batch's own "Retry rows" (History → Bulk batches), which
already knows which of the batch's rows still need it and shows the whole
batch's saved-password state at once, instead of one row in isolation.
`/api/jobs/<id>/retry` (`job_retry`) still handles a `batch_id` job
correctly if called directly (delegates to `_retry_batch_rows`, below, so
the batch's own status/progress stay in sync) — that path just isn't
exposed by any button anymore, kept as a correctness guarantee for the
endpoint itself rather than a reachable UI action.

**Migration history groups a batch's rows into one summary row**
(`renderHistory`/`renderHistoryBatchRow` in `static/js/app.js`) instead of
listing every row individually — a 30-row batch would otherwise flood the
list. Grouping is purely a frontend lookup against the already-loaded
`allBatches` array (matched via each job's `batch_id`), no new endpoint;
a row whose batch isn't loaded yet falls back to showing on its own so
nothing is silently dropped. Clicking the group opens the same read-only
batch-detail ("View rows") modal used from the Bulk batches card — there's
still only that one place to inspect a batch's individual rows.

**Retrying reuses the existing row(s) in place — same job id(s), same
batch id — rather than creating new ones.** `db.reset_job_for_retry` resets
a job row back to `queued` with its stats cleared (used by both the
single-job and batch retry paths, right before `_execute_job` runs again);
`db.reopen_batch_for_retry` does the batch-level equivalent. `_execute_job`
opens `job["log_path"]` in **append** mode, not truncate — writing a
`--- Resumed <timestamp> ---` marker first when the file already has
content — so a job's full log stays one continuous, chronological record
across every attempt instead of losing the previous one; a brand new job's
log file doesn't exist yet, so this is identical to starting fresh either
way. History therefore keeps exactly one row per mailbox/batch no matter
how many times it's retried, instead of accumulating a `(retry) (retry)
(retry)...`-suffixed trail.

Because rows can now be *revisited* (a batch's completed/success/error no
longer only ever moves forward from a fresh 0), `_run_batch_thread`
recomputes those three numbers from the DB after every row
(`db.recompute_batch_progress`, scanning all of a batch's rows by their
current status) instead of tracking them incrementally in a local counter
— an incremental counter seeded at 0 would be wrong for a *partial* retry,
since it has no way to know about rows that already succeeded in a
previous round. `_run_batch_thread`'s `rows` argument (and therefore its
`row_start`/`row_done` broadcasts' own `index`/`total`) reflects only the
current *round* being run (all of a batch's rows on its first run, a
chosen subset on a retry) — separate from the batch's own persisted
`total`, which never changes.

Retrying a single job — by hand via `/api/jobs/<id>/retry` (`job_retry`) —
and retrying a batch's failed/interrupted rows — via
`/api/batches/<id>/retry` (`batch_retry`, the "Retry rows" modal) or
automatically via `_auto_resume_interrupted_batches` (below) — all fall
back to a still-stored "auto-resume" credential (`has_stored_password` on
`/api/jobs` and `/api/batches/<id>/jobs`) when the caller doesn't supply a
password, instead of requiring one every time. The frontend's single
"Resume" button (`openResumeModal`, `#resume-modal`) is the same dialog
whether or not a password is stored — it just labels the password fields
"Saved password" and makes them optional when one is (leaving both blank
reuses it, same idea as the Retry-rows modal's "saved password" rows) —
rather than two different buttons ("Resume" vs. "Resume now") for what's
otherwise the same action; there's also no more separate "jump to the New
migration form and retype everything" path — `#resume-modal` covers both
cases, so `resumeJob`/the old form pre-fill no longer exist. The batch path
goes through the shared `_retry_batch_rows` helper — reset each retried row
(`reset_job_for_retry`, optionally re-storing its credential),
`reopen_batch_for_retry` (which also immediately calls
`recompute_batch_progress`, so the Bulk batches table doesn't show stale
counts until the first retried row finishes), then hand the same `rows`
(now carrying each row's *existing* job id in `_job_id`, not a freshly
generated one) to `_run_batch_thread`.

`loadHistory`'s auto-poll (History tab) only kept refreshing while some
job was `status === "running"` — a row sitting `queued` (its brief state
right after a retry resets it, or most of a large batch waiting its turn)
didn't count, so a very fast retry could visibly get stuck on a stale
status until the user hit Refresh by hand. It now also counts `queued`.

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
alongside its `queued` rows. At `_run_batch_thread`'s end, only rows that
actually finished `success` have theirs purged (nothing left to ever
retry); a row left `error`/`interrupted` — including one Stop just killed
(see below) — keeps its stored password, both so a *future* crash before
anyone gets to it can still auto-resume it, and so `batch_retry` (the
"Retry rows" endpoint) can fall back to it when the client sends a blank
password for that job_id instead of making someone retype it (exposed to
the frontend as each job's `has_stored_password`, via `batch_jobs`).
Skipped entirely (nothing purged, regardless of status) if
`db.get_schedule_by_ref(..., "batch", batch_id)` finds an active
delta-sync schedule for that exact batch_id — that schedule owns an
indefinite copy of every row's credentials instead. On startup,
right after the orphan-interrupt sweep, `_auto_resume_interrupted_batches`
scans **every** `interrupted` batch with still-stored credentials and
silently re-runs its pending rows in place via `_retry_batch_rows` (same
batch, same job ids — see above) — no user action, and *regardless* of
whether a delta-sync schedule also references that batch. Whether a
schedule is attached only affects the cleanup that follows: same as
`_run_batch_thread`, credentials are purged unless `get_schedule_by_ref`
finds one, since that schedule needs them indefinitely for its own future
re-runs. Conflating "should this
resume now" with "does a schedule need these credentials forever" (both
gated on the same schedule check) used to mean a batch with both "Auto-
resume" and "Automatically re-run..." checked wouldn't actually resume
until the schedule's own multi-hour timer came due — the two are
orthogonal (recover-once vs. keep-re-running) and must stay decided
separately. `db.clear_history` also purges any `credential_vault` row
belonging to a job it deletes, so a manual history-clear can't orphan one.

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
