"""
Encryption for the ONE feature in this app that needs to hold onto a
password between requests: scheduled delta sync (see app.py's /api/schedules
routes). Every other code path in this project — a single migration, a bulk
run, Resume — deliberately never stores a password anywhere; this module is
the opt-in exception, used only for jobs/batches where the user explicitly
turned on "scheduled delta sync" and accepted that trade-off.

The key lives in <data dir>/secret.key, created with 0600 permissions the
first time it's needed. Anyone who can read that file AND the database can
decrypt every stored credential — treat it like a root secret:
  - back it up if you rely on scheduled delta sync (losing it makes every
    stored credential permanently undecryptable — schedules would need to
    be re-created with fresh passwords)
  - never commit it, ship it in a support bundle, or loosen its permissions
  - it lives under the data dir, which install.sh already restricts to the
    imapsyncweb service user
"""

import os

from cryptography.fernet import Fernet, InvalidToken

_FERNET = None


def init(data_dir):
    """Call once at startup. Loads the key file, creating it if missing."""
    global _FERNET
    key_path = os.path.join(data_dir, "secret.key")
    if os.path.isfile(key_path):
        with open(key_path, "rb") as f:
            key = f.read()
    else:
        key = Fernet.generate_key()
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    _FERNET = Fernet(key)


def encrypt(plaintext):
    if plaintext is None:
        plaintext = ""
    return _FERNET.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token):
    if not token:
        return ""
    try:
        return _FERNET.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        # Key file was replaced/lost since this was encrypted — surface as
        # an empty password rather than crashing the scheduler loop.
        return ""
