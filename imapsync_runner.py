"""
Builds and runs a real `imapsync` command, streaming its stdout back
line-by-line, and does a best-effort parse of its end-of-run summary.

Security notes:
- Passwords are never put on the command line (visible via `ps aux` to any
  local user). They're written to O_600 temp files and passed via imapsync's
  own --passfile1/--passfile2 flags, then deleted as soon as the process ends.
- Passwords are never written to the per-job log file or echoed back over SSE.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time


class ImapsyncNotFound(RuntimeError):
    pass


def find_imapsync_binary():
    """Look for imapsync on PATH, falling back to a couple of common install spots."""
    found = shutil.which("imapsync")
    if found:
        return found
    for candidate in ("/usr/local/bin/imapsync", "/usr/bin/imapsync", "/opt/imapsync/imapsync"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _write_secret_file(directory, value):
    fd, path = tempfile.mkstemp(dir=directory, prefix="pw_")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600, owner-only
    with os.fdopen(fd, "w") as f:
        f.write(value)
    return path


def build_command(binary, job, password1, password2, secrets_dir):
    """
    job: dict with host1/port1/ssl1/user1/host2/port2/ssl2/user2/options,
    plus optional authuser1/authuser2 for master/admin-account migrations
    (see the "Master / admin account" README section).
    Returns (cmd_list, cleanup_paths).
    """
    pw1_path = _write_secret_file(secrets_dir, password1 or "")
    pw2_path = _write_secret_file(secrets_dir, password2 or "")

    cmd = [
        binary,
        "--host1", job["host1"], "--user1", job["user1"], "--passfile1", pw1_path,
        "--host2", job["host2"], "--user2", job["user2"], "--passfile2", pw2_path,
        "--nolog",       # we capture stdout ourselves into our own per-job log
        "--noreleasecheck",
        "--automap",
    ]
    if job.get("port1"):
        cmd += ["--port1", str(job["port1"])]
    if job.get("port2"):
        cmd += ["--port2", str(job["port2"])]
    cmd.append("--ssl1" if job.get("ssl1") else "--notls1")
    cmd.append("--ssl2" if job.get("ssl2") else "--notls2")

    # Master/admin-account login: --userN stays the mailbox being migrated,
    # --authuserN is who actually authenticates (using passwordN, which is
    # THAT account's password) — imapsync's own support for exactly this
    # "log in as admin, act as user" pattern (Dovecot master user, Zimbra
    # admin auth, Cyrus proxy auth, and similar).
    if job.get("authuser1"):
        cmd += ["--authuser1", job["authuser1"]]
    if job.get("authuser2"):
        cmd += ["--authuser2", job["authuser2"]]

    options = job.get("options") or {}
    if options.get("dry"):
        cmd.append("--dry")
    if options.get("syncflags", True):
        cmd += ["--syncinternaldates", "--syncflags"]
    if options.get("delete2duplicates"):
        cmd.append("--delete2duplicates")
    if options.get("subscribeall"):
        cmd.append("--subscribeall")
    exclude = (options.get("exclude") or "").strip()
    if exclude:
        for name in [n.strip() for n in exclude.split(",") if n.strip()]:
            cmd += ["--exclude", name]

    return cmd, [pw1_path, pw2_path]


_ERR_RE = re.compile(r"Detected (\d+) errors?", re.IGNORECASE)
_EXIT_RE = re.compile(
    r"Exiting with return value (\d+)\s*\(([^)]*)\)\s*(\d+)/(\d+)\s*nb_errors/max_errors",
    re.IGNORECASE,
)
_SYNCED_RE = re.compile(
    r"all (\d+) identified messages in host1 are on host2", re.IGNORECASE
)
_MSG_COPY_RE = re.compile(r"^\s*\d+/\d+\s+Msg", re.MULTILINE)
_FOLDER_RE = re.compile(r"^\+\+\+ Folder\s+\[(.+?)\]", re.MULTILINE)


def parse_summary(log_text, returncode):
    """
    Best-effort extraction of stats from imapsync's own stdout. imapsync's
    exact wording has drifted across releases, so every field here is
    "best effort, verify against the full log if it looks off" rather than
    guaranteed-accurate — the full raw log is always saved alongside it.
    """
    errors = None
    m = _ERR_RE.search(log_text)
    if m:
        errors = int(m.group(1))

    m = _EXIT_RE.search(log_text)
    exit_errors = None
    if m:
        exit_errors = int(m.group(3))
    if errors is None:
        errors = exit_errors

    messages = None
    m = _SYNCED_RE.search(log_text)
    if m:
        messages = int(m.group(1))
    if messages is None:
        # Fall back to counting per-message "N/M Msg ..." progress lines.
        count = len(_MSG_COPY_RE.findall(log_text))
        messages = count or None

    folders = len(set(_FOLDER_RE.findall(log_text))) or None

    if returncode == 0 and (errors or 0) == 0:
        status = "success"
    else:
        status = "error"

    # On a successful run, imapsync only prints an "N identified messages"/
    # per-message copy line when there was at least one message to sync —
    # a delta run that finds nothing new to copy prints neither, which
    # previously left `messages` as None (shown as "–" in the UI) even
    # though 0 is the accurate count. Mirror the same success-implies-0
    # fallback already used for `errors` below.
    if messages is None and status == "success":
        messages = 0

    return {
        "status": status,
        "folders": folders,
        "messages": messages,
        "data_mb": None,  # imapsync doesn't print a single reliable total-bytes line
        "errors": errors if errors is not None else (0 if status == "success" else None),
    }


def run_imapsync(binary, job, password1, password2, log_fp, on_line):
    """
    Runs imapsync, writing every line to log_fp and calling on_line(line) for
    each one as it arrives (used to push SSE events). Returns
    (returncode, full_log_text, elapsed_seconds).
    """
    secrets_dir = tempfile.mkdtemp(prefix="imapsync-web-secrets-")
    try:
        cmd, secret_paths = build_command(binary, job, password1, password2, secrets_dir)
        started = time.time()

        # gunicorn sets SERVER_SOFTWARE in its own process environment (for
        # its own WSGI purposes), which every subprocess inherits by default.
        # imapsync treats a present SERVER_SOFTWARE as "I'm running as a real
        # CGI script" and tries `require CGI` — a module we neither install
        # nor want here (it would also wrap imapsync's plain stdout in HTTP
        # headers/cookies, breaking our log streaming and summary parsing).
        # Strip it so imapsync always sees itself running in plain "Standard"
        # context, regardless of which WSGI server hosts this app.
        child_env = os.environ.copy()
        child_env.pop("SERVER_SOFTWARE", None)

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=child_env,
        )
        lines = []
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            log_fp.write(line + "\n")
            log_fp.flush()
            on_line(line)
        proc.wait()
        elapsed = time.time() - started
        return proc.returncode, "\n".join(lines), elapsed
    finally:
        for p in os.listdir(secrets_dir):
            try:
                os.remove(os.path.join(secrets_dir, p))
            except OSError:
                pass
        try:
            os.rmdir(secrets_dir)
        except OSError:
            pass
