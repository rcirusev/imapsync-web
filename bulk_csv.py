"""
CSV parsing for bulk migrations.

Every row is fully self-contained (its own host/port/ssl/options), so a
single file can migrate users onto completely different destination servers
if needed. The uploaded file is only ever held in memory for the duration of
parsing + the batch run — it is never written to disk.
"""

import csv
import io
import json

REQUIRED_FIELDS = ["host1", "user1", "password1", "host2", "user2", "password2"]

# authuser1/authuser2 are optional: leave blank for a normal per-mailbox
# login. Fill one in to migrate that side via a master/admin account instead
# — user1/user2 stays the mailbox being migrated, authuser1/authuser2 is who
# actually authenticates, and password1/password2 becomes THAT account's
# password (the same one for every row using it, typically). See the
# "Master / admin account" README section.
TEMPLATE_HEADER = [
    "host1", "port1", "ssl1", "user1", "authuser1", "password1",
    "host2", "port2", "ssl2", "user2", "authuser2", "password2",
    "dry", "syncflags", "delete2duplicates", "subscribeall", "exclude",
]


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    """
    Neutralizes formula/DDE injection before a value is written into a CSV
    cell. Excel/LibreOffice (and older DDE-capable spreadsheet apps) treat a
    cell that starts with =, +, -, @ (or a leading tab/CR) as a formula, not
    text. Every value written by build_rows_csv ultimately traces back to a
    hostname/username/folder name someone typed into the New migration form
    or a bulk CSV upload, so prefix a leading apostrophe onto anything that
    would otherwise be read back as a formula — spreadsheet apps then always
    display/import it as plain text.
    """
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(_FORMULA_PREFIXES) else s


def to_bool(value, default):
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def build_template_csv():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TEMPLATE_HEADER)
    writer.writerow([
        "imap.oldprovider.com", "993", "true", "alice@old.com", "", "secret1",
        "imap.newprovider.com", "993", "true", "alice@new.com", "", "secret2",
        "false", "true", "false", "true", "Spam,Trash",
    ])
    writer.writerow([
        "imap.oldprovider.com", "993", "true", "bob@old.com", "", "secret3",
        "imap.newprovider.com", "993", "true", "bob@new.com", "", "secret4",
        "false", "true", "false", "true", "",
    ])
    return buf.getvalue()


def build_rows_csv(jobs):
    """
    Builds a CSV in the same column order as the template, pre-filled from a
    list of job rows (as returned by db.list_jobs_by_batch / db.list_jobs) —
    used for the "download failed rows" retry flow on a batch. Passwords are
    deliberately left blank (never stored anywhere) for the user to re-enter
    before re-uploading via the normal Bulk tab.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TEMPLATE_HEADER)
    for job in jobs:
        try:
            options = json.loads(job.get("options_json") or "{}")
        except (TypeError, ValueError):
            options = {}
        writer.writerow([
            _csv_safe(job.get("host1", "")), job.get("port1", ""), "true" if job.get("ssl1") else "false",
            _csv_safe(job.get("user1", "")), _csv_safe(job.get("authuser1") or ""), "",
            _csv_safe(job.get("host2", "")), job.get("port2", ""), "true" if job.get("ssl2") else "false",
            _csv_safe(job.get("user2", "")), _csv_safe(job.get("authuser2") or ""), "",
            "true" if options.get("dry") else "false",
            "true" if options.get("syncflags", True) else "false",
            "true" if options.get("delete2duplicates") else "false",
            "true" if options.get("subscribeall", True) else "false",
            _csv_safe(options.get("exclude", "")),
        ])
    return buf.getvalue()


def parse_bulk_csv(file_bytes):
    """
    Returns (rows, row_errors).
    rows: list of job dicts ready for the runner (host1/port1/ssl1/user1/
          password1/host2/port2/ssl2/user2/password2/options).
    row_errors: list of {"row": n, "reason": "..."} for rows that were
                skipped (n counts from 1 = header, so data row 1 is n=2).
    """
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], [{"row": 0, "reason": "File is not valid UTF-8 text."}]

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], [{"row": 0, "reason": "Empty or unreadable CSV file."}]

    missing_cols = [c for c in REQUIRED_FIELDS if c not in reader.fieldnames]
    if missing_cols:
        return [], [{
            "row": 0,
            "reason": f"Missing required column(s): {', '.join(missing_cols)}. "
                      f"Download the template for the expected format.",
        }]

    rows, row_errors = [], []
    for i, raw in enumerate(reader, start=2):  # row 1 is the header
        def get(key, default=""):
            return (raw.get(key) or "").strip() or default

        missing = [f for f in REQUIRED_FIELDS if not get(f)]
        if missing:
            row_errors.append({"row": i, "reason": f"Missing value(s): {', '.join(missing)}"})
            continue

        rows.append({
            "host1": get("host1"), "port1": get("port1", "993"),
            "ssl1": to_bool(raw.get("ssl1"), True),
            "user1": get("user1"), "authuser1": get("authuser1") or None,
            "password1": get("password1"),
            "host2": get("host2"), "port2": get("port2", "993"),
            "ssl2": to_bool(raw.get("ssl2"), True),
            "user2": get("user2"), "authuser2": get("authuser2") or None,
            "password2": get("password2"),
            "options": {
                "dry": to_bool(raw.get("dry"), False),
                "syncflags": to_bool(raw.get("syncflags"), True),
                "delete2duplicates": to_bool(raw.get("delete2duplicates"), False),
                "subscribeall": to_bool(raw.get("subscribeall"), True),
                "exclude": get("exclude", ""),
            },
        })

    return rows, row_errors
