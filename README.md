# imapsync Web UI

A self-hosted web front-end for [imapsync](https://github.com/imapsync/imapsync).
Runs the real `imapsync` binary, streams its real output live to the
browser, and keeps a history of every migration (source/destination,
status, best-effort stats, full log) in a small local SQLite database — so
one install can be reused by one person for many migrations, or by a team.
It supports both a single migration through a form, and a **bulk migration
from a CSV file** for migrating many mailboxes in one go.

## Quick install (recommended)

On any Debian/Ubuntu or RHEL/Fedora-family Linux server:

```bash
sudo ./install.sh
```

This will, in order:

1. Install `imapsync` itself if it isn't already on the system (via the
   distro package first, falling back to building it from source).
2. Install Python 3 + venv if needed.
3. Create a dedicated, unprivileged system user (`imapsyncweb`) to run the
   service.
4. Copy this project to `/opt/imapsync-web` (override with `INSTALL_DIR=...`).
5. Create a Python virtualenv and install dependencies into it.
6. Register and start a **systemd service** (`imapsync-web`) that starts on
   boot and restarts on failure, served by gunicorn on port `8000` (override
   with `PORT=...`).

Re-running `install.sh` later is safe — it updates the files in place and
restarts the service, so it's also how you deploy updates.

```bash
sudo INSTALL_DIR=/srv/imapsync-web PORT=9000 ./install.sh
```

Once installed:

```bash
systemctl status imapsync-web
systemctl restart imapsync-web
journalctl -u imapsync-web -f
```

### Manual / dev run (no systemd)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000
```

You still need `imapsync` itself installed and on `PATH` for real runs —
see [the imapsync install docs](https://github.com/imapsync/imapsync) if you
don't want to use `install.sh`'s automatic install. Without it, the app
still runs and shows a clear "imapsync not found" banner instead of letting
you start a migration.

## Docker

Prefer a container over `install.sh`'s systemd install? Nothing else to
install on the host — the image bundles `imapsync` itself, built from
source at image-build time using the exact same package list/steps as
`install.sh`'s Debian/Ubuntu fallback (see `Dockerfile`), together with
this app.

```bash
git clone https://github.com/rcirusev/imapsync-web.git
cd imapsync-web
cp .env.example .env        # optional: set IMAPSYNC_WEB_USERNAME/PASSWORD
docker compose up -d
```

Then open <http://localhost:8000>. History, per-job logs, and the
delta-sync secret key all persist in the named `imapsync_data` volume
across restarts and upgrades (`git pull && docker compose up -d --build`).

Plain `docker`, no compose:

```bash
docker build -t imapsync-web .
docker run -d --name imapsync-web -p 8000:8000 \
  -v imapsync_data:/data \
  -e IMAPSYNC_WEB_USERNAME=admin -e IMAPSYNC_WEB_PASSWORD='change-me' \
  imapsync-web
```

### Pre-built image (no clone/build needed)

`.github/workflows/docker-publish.yml` builds this image and publishes it to
GHCR (GitHub Container Registry) on every push to `main`. Once published,
any machine with Docker can run it directly:

```bash
docker run -d --name imapsync-web -p 8000:8000 \
  -v imapsync_data:/data \
  -e IMAPSYNC_WEB_USERNAME=admin -e IMAPSYNC_WEB_PASSWORD='change-me' \
  ghcr.io/rcirusev/imapsync-web:latest
```

No source checkout, no local build — this pulls the same image every
install builds from `Dockerfile`. Upgrade with
`docker pull ghcr.io/rcirusev/imapsync-web:latest && docker compose up -d`
(or the equivalent `docker run`, recreating the container).

`.github/workflows/check-upstream.yml` runs daily and watches
[imapsync/imapsync](https://github.com/imapsync/imapsync) for new commits on
its default branch. When one lands, it records the new commit SHA in
`IMAPSYNC_UPSTREAM_SHA.txt` and pushes that — which triggers
`docker-publish.yml` to rebuild and republish the image (the Dockerfile
always clones imapsync's current tip, so the rebuild picks up the update
automatically). No manual step needed to stay current with upstream; run
`docker compose pull && docker compose up -d` periodically to pick up
whatever's been published.

The same security notes as the systemd install apply here — see
"Authentication" and "Security notes before you expose this to anyone but
yourself" below. This still serves plain HTTP on the mapped port: put a
reverse proxy with HTTPS in front of it before exposing it beyond
localhost/a trusted LAN, and set `IMAPSYNC_WEB_USERNAME`/
`IMAPSYNC_WEB_PASSWORD` (via `.env` or `-e`) unless the container is only
reachable by people who should have access.

## What's real vs. best-effort

- **The migration itself is real.** Every "Start migration" click runs the
  actual `imapsync` binary with the accounts and options from the form, and
  the console shows its real, unmodified stdout live.
- **Passwords are never stored or logged, by default.** They're sent once
  over the `/api/start` request to kick off the job, written to a temp file
  with `0600` permissions for `imapsync`'s own `--passfile1`/`--passfile2`
  flags (so they never appear in `ps aux` either), and the temp file is
  deleted as soon as the job ends. They are never written to the per-job
  log. The two explicit opt-in exceptions, both encrypted at rest, are
  **Scheduled delta sync** (below) and a bulk batch's **Auto-resume if the
  server restarts mid-batch** checkbox (see Bulk migration below) — off
  unless you turn them on.
- **The summary stat tiles (folders / messages / errors) are best-effort.**
  imapsync's exact log wording has drifted across releases, so these are
  parsed with a few regexes against common phrasing (see
  `imapsync_runner.py::parse_summary`) and can show "–" if nothing matched.
  The **full raw log is always saved** and viewable/downloadable from the
  History tab — treat that as the source of truth, the stat tiles as a
  convenience.
- **Progress is indeterminate**, not a percentage — imapsync doesn't expose
  a single reliable "N% done" figure, so the UI shows an animated bar while
  a job runs rather than inventing a number.
- **Running under gunicorn specifically (not the Flask dev server) requires
  one env-var workaround, already applied.** gunicorn sets `SERVER_SOFTWARE`
  in its own process environment, which subprocess children inherit by
  default; imapsync treats a present `SERVER_SOFTWARE` as "I'm being run as
  a real CGI script" and tries to `require CGI` — a module this project
  deliberately doesn't install (and don't want active: CGI mode also wraps
  imapsync's stdout in HTTP headers/cookies, which would break log
  streaming and summary parsing). `imapsync_runner.py::run_imapsync` strips
  `SERVER_SOFTWARE` from the subprocess's environment before launching
  imapsync, so it always runs in plain "Standard" context regardless of
  which WSGI server hosts this app.
- **Runs as a single gunicorn worker process, on purpose.** Both
  `install.sh` and the Dockerfile use `--workers 1 --threads 8` — live
  progress (SSE), Stop, and auto-resume all rely on plain in-memory state
  that only one process shares. Raising `--workers` above 1 would split
  that state across processes that don't talk to each other, so a Stop
  click or live log view can silently land on the "wrong" process and do
  nothing. `--threads 8` already gives real concurrency (many simultaneous
  requests/SSE connections) within that one process, without that split.

## Testing a connection before you migrate

The **Test connection** button on the New migration form checks that both
accounts actually log in — before you commit to a real (or even dry) run.
It's a plain IMAP `LOGIN` against host1 and host2 in parallel, done with
Python's own `imaplib` rather than `imapsync` itself, so it takes a couple
of seconds instead of however long a full connect-and-scan would take, and
it reports the real underlying error (DNS failure, connection refused,
timeout, or the server's own login-rejected message) next to whichever
side failed. Like every password field in this app, the ones used for this
check are sent once and never stored or logged — nothing here touches the
database.

It doesn't cover OAuth2 accounts (see the OAuth2 note under Security notes
below) and it isn't a substitute for the real run — a login can succeed and
a migration can still hit per-folder errors — but it turns "start a full
migration to find out the password was wrong" into a five-second check,
which is exactly the kind of thing that would have saved a round-trip the
first time an authentication failure showed up in this project. It also
respects the master/admin account setting below, so you can verify an
admin login works before running a whole batch through it.

## Master / admin account

If you administer the mail server(s) yourself, you don't have to ask every
individual mailbox owner for their password to migrate them. **"Use a
master/admin account"**, on either side of the New migration form, sends
the mailbox's username as before but authenticates with a separate
admin/master account instead — a plain admin username field appears, and
the password field switches to meaning that admin account's password, not
the individual mailbox's.

This only works if the mail server itself supports it — the app can't
force it. It's a real, common capability, just configured server-side, not
in this app:

- **Dovecot** (the most common choice for self-hosted / cPanel-style Linux
  mail): the "master user" feature — a master password (or a per-admin
  password) configured in `dovecot-master-users` / your auth config, after
  which any mailbox can be accessed by authenticating as the master user.
- **Zimbra**: admin auth / delegated admin access to any mailbox.
- **Cyrus IMAP**: proxy authentication (`imapd.conf`'s `admins`/`proxy` config).
- **Microsoft 365 / Exchange**: a different mechanism entirely (Application
  Impersonation or app-only OAuth2, not IMAP master-user) — not covered by
  this feature; see the OAuth2 note under Security notes.

Under the hood this maps straight onto imapsync's own `--authuser1` /
`--authuser2` flags, and the same idea for the Bulk (CSV) tab: the
downloadable template now has optional `authuser1`/`authuser2` columns.
Leave them blank for a normal per-mailbox login; fill one in and that row's
`password1`/`password2` becomes the admin account's password instead of
that specific mailbox's — since one admin account is usually shared across
every row, most bulk files will have the same `authuser1`/`authuser2`
value repeated down the whole column with a `password1`/`password2` that
matches. A job that used this shows a small **master** tag next to its
account in History.

The admin credentials get exactly the same handling as any other password
in this app: sent once to start the migration (or the connection test),
never written to the log or the database — unless you explicitly turn on
scheduled delta sync for that job, which is the one place any password is
stored (see Scheduled delta sync above).

## Watching a running job, and resuming after an interruption

**Watching:** a migration runs as a background thread on the server,
completely independent of your browser — closing the tab, losing wifi, or
your laptop sleeping does not stop it. To check on it:

- The **New migration** tab automatically reattaches to whatever job it
  last started, even after a page reload or a dropped connection — it
  remembers the job id and reconnects on its own, replaying the log so far
  and then continuing live. If the connection drops mid-migration you'll
  briefly see "Reconnecting…"; it keeps retrying for a couple of minutes
  before giving up for good.
- The **History** tab shows every job, live: while any job is "Running" it
  auto-refreshes every few seconds, and clicking **View log** on a running
  job tails its real output live instead of a stale snapshot — from any
  browser, any tab, any time.

**Resuming after an interruption:** if the whole server process gets
interrupted mid-migration (a `systemctl restart`, a VM reboot, a crash) the
background thread and the `imapsync` subprocess die with it. On the next
startup, this app marks any job that was still "Running" as **Interrupted**
in History (rather than leaving a phantom "Running" row forever) — nothing
is silently lost, but that specific run never got a final result.

To continue, an Interrupted (or Failed) row in History that isn't part of
a bulk batch (see Bulk migration below for those — they retry from their
batch instead) gets a **Resume** button. It opens a small dialog showing
the source/destination and a password field for each; if a password is
still stored for that row (from **Keep resumable** — e.g. it was killed
via a batch's Stop button rather than lost to a crash), those fields are
optional — leave them blank to reuse it, or type new ones to override it
just this once. Either way, clicking Resume re-launches the job **in
place** — same row, log appended to (not replaced) — instead of a
checkpoint/resume feature this app built; it, like everything else here
that "resumes" something, just relies on `imapsync` itself being
incremental: it checks what already exists on the destination and only
transfers what's missing, so re-running the same migration doesn't
re-copy anything that already made it across. This is also the right way
to periodically re-sync an account (e.g. run it again a day later to pick
up new mail) — same button, same idea.

## Bulk migration from a CSV file

The **Bulk (CSV)** tab migrates many mailboxes in one run instead of filling
the form in one at a time:

1. Click **Download the CSV template** to get a file with the exact header
   row and two example rows.
2. Every row is a **fully independent migration** — its own source and
   destination host/port/SSL/user/password, and its own options
   (`dry`, `syncflags`, `delete2duplicates`, `subscribeall`, `exclude`).
   Only `host1, user1, password1, host2, user2, password2` are required;
   everything else falls back to the same defaults as the New migration
   form if left blank. A folder list in `exclude` with more than one name
   needs the field quoted, e.g. `"Spam,Trash"` (the template shows this).
   `authuser1`/`authuser2` are optional too — see Master / admin account
   below for migrating every row through an admin login instead of asking
   each mailbox owner for their password.
3. Upload the file and click **Start bulk migration**. Rows run
   **sequentially, one at a time**, in file order — not in parallel — so a
   large batch doesn't open a burst of simultaneous connections against the
   source/destination servers. Any row missing a required column is skipped
   up front and listed as a warning; every other row still runs.
4. The console shows the real, live imapsync output for whichever row is
   currently running; a results table fills in below as each row finishes.
   Every row also lands in the regular **History** tab exactly like a single
   migration would (tagged "BULK"), so it's never lost even if you navigate
   away mid-run.

The CSV is parsed entirely in memory and is never written to disk — same
password handling as a single migration (see below), just once per row
instead of once per click.

### Tracking a large batch (e.g. 100 mailboxes)

While the Bulk tab is open, you already get a live progress line (which row
is running now) and a results table that fills in as each mailbox
finishes. But you don't have to keep that tab open: the **History** tab has
a **Bulk batches** table that shows every batch's `completed/total`,
success count, and error count, auto-refreshing every few seconds while
it's running — so you can check how far a 100-row run has gotten from any
browser, at any time, without babysitting the Bulk tab. Each individual
mailbox also still shows as its own row (tagged "BULK") in the regular
history table below it, with its own log and its own **Resume** button if
it failed.

If the whole server process is interrupted partway through a batch (a
restart, a crash, a reboot), the batch is marked **Interrupted** on the next
startup rather than showing "Running" forever, and it keeps the exact
progress it had reached (e.g. "42/100"). Every row — including ones that
hadn't gotten their turn to run yet — already has its host/port/SSL/
username/options recorded in the database from the moment the batch
started (passwords excluded by default), so nothing is lost even for a row
that never actually started `imapsync`: it's marked **Interrupted** too, on
the next startup, right alongside rows that failed mid-run.

What happens next depends on whether **Keep resumable** was checked when
the batch was started:

- **Checked:** every still-pending row's password was kept encrypted for
  exactly this. On the very next startup, before anything else, this app
  automatically re-runs those specific rows **in place** — same batch, no
  one needs to do anything, and History doesn't grow a new entry for it.
  Those temporary passwords are deleted the moment the batch next finishes
  (or, sooner, the moment `clear_history` removes the row). If a
  delta-sync schedule is also attached to the batch, it still resumes
  immediately regardless — that schedule re-running it later, on its own
  timer, is a separate thing.
- **Unchecked (the default):** nothing was kept, so nothing can resume on
  its own. Use **Retry rows** or **Download failed CSV** (see Batches
  below) by hand to get every row that still needs a rerun — started or
  not — instead of re-uploading the entire original file.

### Batches, like Office 365 migration batches

Each CSV upload works like a named, self-contained migration batch — the
same idea as an Exchange/Office 365 IMAP migration batch:

- **Name it.** The Bulk tab has an optional "Batch name" field (e.g.
  "Finance dept — March 2026"); it shows up in the History → Bulk batches
  table instead of a bare id, so a list of past batches actually reads like
  a list of migrations you ran, not a pile of UUIDs.
- **Drill into one batch.** Clicking **View rows** on a batch opens just its
  mailboxes — status, message/error counts, per-row log — instead of
  scrolling through every job from every batch mixed together. It's
  read-only (retrying happens through **Retry rows**, below, which already
  knows which rows still need it and shows the whole batch's saved-password
  state at once).
- **Stop a running batch.** The **Stop** button (in the Bulk tab's console,
  and next to any "Running" batch in History) stops picking up any row
  that hasn't started yet, and also terminates whichever row is currently
  in flight right now — it doesn't wait for that transfer to finish on its
  own. Killing mid-transfer is safe: `imapsync` is incremental, so
  whatever already copied over stays copied, and re-running the same row
  (**Retry rows**, below) just picks up the rest. The batch is then marked
  **Stopped**, distinct from a crash-induced **Interrupted**.
- **Retry only the failed rows.** Once a batch has any errors, was stopped,
  or was interrupted, two ways to rerun just what still needs it (never the
  whole original file) show up next to it in History → Bulk batches:
    - **Retry rows** — the primary way — opens those rows right in the
      browser — host/source shown per row, a password field for each,
      **Retry selected rows** re-runs them **in place** (same batch, same
      rows — nothing new added to History). Nothing ever touches a file;
      the passwords go straight from that form to the retry, the same as
      any password field in this app. A row tagged **"saved password"**
      (it still has one stored from **Keep resumable**, e.g. it was killed
      via Stop rather than lost to a crash) can be left blank to reuse it
      instead of retyping it — and the modal's own **Keep resumable**
      checkbox defaults to checked whenever any row being retried already
      had one, so that protection carries forward on its own across
      repeated stop/retry cycles instead of needing to be re-enabled by
      hand every time.
    - **or CSV**, next to it — a lower-key alternative for when you'd
      rather edit the list in a spreadsheet first, or hand it off to
      someone else to fill in. Produces the same set of rows as a CSV,
      with host/port/SSL/username/options carried over and passwords left
      blank, to fill in and re-upload via the normal Bulk tab.

## Scheduled delta sync

Between an initial migration and final cutover, mailboxes on the source
server keep receiving new mail. **Scheduled delta sync** re-runs a
migration (or a whole bulk batch) automatically on a timer, so the
destination keeps catching up — imapsync is incremental, so every re-run
only copies what's new, not the whole mailbox again.

Turn it on with the **"Automatically re-run this migration..."** checkbox
on the New migration form, or the equivalent one on the Bulk tab, and set
an interval (in hours, minimum 15 minutes). Manage every active schedule
from **History → Scheduled delta syncs**: it shows the accounts/batch,
interval, last/next run, and has **Edit**, **Run now** (trigger
immediately, without waiting) and **Delete** (turn it off) buttons. Every
automatic run shows up in the normal History/Bulk batches tables exactly
like a manual one, tagged **AUTO** so you can tell them apart.

**Edit** changes the interval in place (the countdown to the next run
restarts from the moment you save, using the new interval) without
deleting and re-creating the schedule. For a single-migration schedule it
can also update the stored password(s) — leave a password field blank to
keep what's already stored. A batch schedule can only have its interval
edited from here: a batch covers many accounts that usually don't share
one password, so changing batch credentials still means re-uploading the
CSV.

**This is the one place in the app that stores a password.** Every other
flow (a single migration, a bulk run, Resume) deliberately never persists a
credential — but a schedule needs to be able to log back in on its own,
with nobody there to type a password in. Turning on scheduled delta sync
encrypts and stores that job's (or every row's, for a batch) passwords in
the local database, using a key file at `<data dir>/secret.key` that's
created automatically with `0600` permissions. A few things follow from
that:

- It's opt-in per migration/batch — nothing is stored unless you explicitly
  turn this on for that specific job.
- Deleting a schedule immediately purges its stored credentials, not just
  the schedule itself.
- The key file is as sensitive as a root secret: anyone who can read it
  *and* the database can decrypt every stored password. It's already
  covered by the same data-directory protection as the rest of `data/` (see
  Security notes below) — just be aware backing up `data/` means backing up
  key material, and losing the key file makes every stored credential
  permanently undecryptable (schedules would need to be re-created with
  fresh passwords).
- A schedule always re-runs the *exact* accounts/options from when it was
  created — only the interval and (for a single migration) the password can
  be changed via Edit. If the accounts themselves need to change, delete
  the schedule and set up a fresh one.

## Searching and filtering History

Both the **Bulk batches** and **Migration history** tables have a search
box and a status dropdown above them. The search box matches on batch name
(Bulk batches) or on either account's user/host (Migration history); the
dropdown narrows to one status. Both filter instantly against whatever was
last loaded — no extra request per keystroke — and combine with each
other (e.g. status = Failed + a search term at once). They don't affect
what's fetched or what Clear history considers, only what's currently
shown.

## Clearing old history

History and Bulk batches build up over time, especially with delta sync
running automatically every few hours. **Migration history → Clear
history** removes finished single migrations and finished bulk batches in
one go, deleting their DB rows and their log files under `data/logs/`.

It's safe to click at any time: a row that's still `running` is never
touched, and neither is any job or batch that an active delta-sync
schedule still points at — deleting those out from under a schedule would
make its next automatic run silently find nothing and do nothing.
Everything else (completed, failed, interrupted, stopped) gets removed.
There's a confirmation prompt before anything is deleted, and no undo
afterwards.

## Project layout

```
app.py                  Flask app: routes, SSE streaming, job + batch + schedule orchestration
imapsync_runner.py      Builds the real imapsync command, runs it, parses output
bulk_csv.py             CSV parsing + validation for the Bulk tab, template generator
db.py                   SQLite history (stdlib sqlite3, no extra dependency)
crypto_store.py         Encrypts/decrypts passwords for scheduled delta sync only
conn_test.py             Login-only IMAP check for the Test connection button
templates/index.html    New-migration form + Bulk (CSV) tab + History tab
static/css/style.css    Styling (light/dark)
static/js/app.js        Form handling, SSE consumption, bulk upload, history, log viewer
install.sh              Universal installer (imapsync + venv + systemd service)
Dockerfile               Container image: builds imapsync from source + this app, runs under gunicorn
docker-compose.yml      One-command run (build + persistent volume + optional Basic Auth via .env)
.env.example            Template for docker compose's optional IMAPSYNC_WEB_USERNAME/PASSWORD
data/                   Created at runtime: SQLite DB, per-job logs, secret.key (gitignore this)
```

## Authentication

The app can require HTTP Basic Auth on every route (pages, API, static
files alike) — off by default, so an existing install keeps working
unchanged until you opt in.

**Easiest way — let the installer ask you.** Run `install.sh` with no
`IMAPSYNC_WEB_USERNAME`/`IMAPSYNC_WEB_PASSWORD` set and, if it's running in an
actual terminal (not piped through a non-interactive shell), it will ask:

```
sudo ./install.sh
...
[install] Set up a login (HTTP Basic Auth) for the web UI? [y/N]: y
  Username: admin
  Password:
  Confirm password:
[install] Login will be enabled for user 'admin'.
```

Typing the password at a prompt (rather than putting it on the command line)
also sidesteps shell-quoting issues — special characters like `&`, `#`, `)`
in the password are read exactly as typed and never touch the shell's own
parsing. Answering `N` (or just pressing Enter) leaves the app open, same as
before. Re-running `install.sh` later on an already-installed instance asks
again, so you can turn login on (or change it) at any time this way.

**Non-interactive / scripted installs** — pass the two environment variables
instead, and the prompt is skipped automatically:

```
IMAPSYNC_WEB_USERNAME=admin
IMAPSYNC_WEB_PASSWORD=some-long-random-value
```

With `install.sh`, pass them on the same command line as everything else —
**wrap the password in single quotes** if it contains any shell-special
characters (`&`, `#`, `$`, `!`, spaces, etc.), otherwise bash will mangle the
command before `install.sh` ever sees it:

```
sudo IMAPSYNC_WEB_USERNAME=admin IMAPSYNC_WEB_PASSWORD='some-long-random-value' ./install.sh
```

The installer writes both into the systemd unit's `Environment=` lines and
locks the unit file down to `chmod 600` (root-only) since it now holds a
plaintext secret — systemd unit files are world-readable by default
otherwise. To turn auth on for an already-installed instance, re-run
`install.sh` the same way, or edit
`/etc/systemd/system/imapsync-web.service` directly and
`systemctl daemon-reload && systemctl restart imapsync-web`.

Both variables must be set together — setting only one leaves the app open
(with a warning logged at install time) rather than locking you out with a
half-configured password. This is plain Basic Auth: it stops casual/opportunistic
access but the credentials go over the wire in a (trivially reversible)
encoded form, not encrypted — put the app behind HTTPS (see Security notes
below) if it's reachable over an untrusted network, so Basic Auth
credentials aren't sent in the clear.

## Security notes before you expose this to anyone but yourself

This app now handles real IMAP credentials and shells out to a real binary.
Before you put it in front of other people:

- **Put it behind HTTPS.** `install.sh` runs plain HTTP on `localhost`/LAN by
  design — add an nginx (or Caddy/Traefik) reverse proxy with a real
  certificate in front of it.
- **Add authentication.** The app has built-in HTTP Basic Auth — see
  "Authentication" below for how to turn it on. It's off by default, so an
  existing install stays exactly as open as before until you set it. If you'd
  rather handle it outside the app: a reverse-proxy Basic Auth layer, or
  restricting access to a VPN/SSH tunnel/allow-listed IPs via firewall, both
  work fine instead.
- **Restrict the data directory.** `data/` contains per-job logs; those logs
  never contain passwords, but they do contain the hostnames/usernames
  involved in each migration — treat that directory with the same care as
  any other operational log. If you use **scheduled delta sync**, `data/`
  also contains `secret.key` and the database's encrypted credentials for
  every job/batch with a schedule turned on — restrict and back up
  accordingly (see the Scheduled delta sync section above).
- **For OAuth2 accounts** (Gmail / Microsoft 365 with app passwords
  disabled), imapsync supports `--oauthaccesstoken1/2` — this UI doesn't
  collect an OAuth token yet; that's the natural next feature to add if you
  need it (see `imapsync_runner.py::build_command`).

## Extending it

- `imapsync_runner.py::build_command` is where form options map to imapsync
  CLI flags — add more of imapsync's ~300 flags here as form fields if you
  need them (e.g. `--maxage`, `--minsize`, `--gmail1`/`--gmail2` presets).
- `db.py` is a thin, dependency-free SQLite layer — swap it for
  SQLAlchemy/Postgres if you outgrow a single-file database.
- Jobs currently run as background threads with no concurrency cap; for
  heavy multi-user use, consider a real job queue (Celery/RQ + Redis) so
  migrations queue instead of all launching at once.
