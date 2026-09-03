"""
Lightweight "does this login work at all" check, independent of imapsync.

Deliberately doesn't shell out to imapsync --justconnect: doing the LOGIN
directly with the stdlib imaplib client is faster (no process spawn), gives
a real Python exception we can turn into a plain-English message, and needs
no temp passfile — the password lives only in memory for the few hundred
milliseconds this takes, and is never written to disk or logged.
"""

import imaplib
import socket

DEFAULT_TIMEOUT = 10


def test_login(host, port, use_ssl, user, password, timeout=DEFAULT_TIMEOUT, authuser=None):
    """
    Returns {"ok": bool, "message": str}. Never raises.

    If authuser is given, this logs in AS authuser (using `password`, i.e.
    authuser's own password) but acts on `user`'s mailbox — the same
    master/admin-account pattern imapsync's --authuser1/--authuser2 flags
    use (SASL PLAIN with a distinct authzid), for servers that support it
    (Dovecot master user, Zimbra admin auth, etc).
    """
    if not host or not user:
        return {"ok": False, "message": "Host and username are required."}

    try:
        port = int(port) if port else (993 if use_ssl else 143)
    except (TypeError, ValueError):
        return {"ok": False, "message": f"Invalid port: {port!r}"}

    conn = None
    try:
        if use_ssl:
            conn = imaplib.IMAP4_SSL(host, port, timeout=timeout)
        else:
            conn = imaplib.IMAP4(host, port, timeout=timeout)
    except socket.timeout:
        return {"ok": False, "message": f"Connection to {host}:{port} timed out."}
    except socket.gaierror as e:
        return {"ok": False, "message": f"Can't resolve {host}: {e}"}
    except (ConnectionRefusedError, OSError) as e:
        return {"ok": False, "message": f"Can't connect to {host}:{port}: {e}"}
    except Exception as e:  # imaplib can raise its own errors on the handshake
        return {"ok": False, "message": f"Connection failed: {e}"}

    try:
        if authuser:
            # RFC 4616 SASL PLAIN: authzid \0 authcid \0 password — logs in
            # as authuser (authcid) with authuser's password, but acts as
            # `user` (authzid).
            def _plain(_challenge, _u=user, _au=authuser, _pw=password or ""):
                return f"{_u}\x00{_au}\x00{_pw}".encode("utf-8")
            conn.authenticate("PLAIN", _plain)
        else:
            conn.login(user, password or "")
        return {"ok": True, "message": "Login successful."}
    except imaplib.IMAP4.error as e:
        return {"ok": False, "message": f"Login rejected: {e}"}
    except socket.timeout:
        return {"ok": False, "message": "Login timed out."}
    except Exception as e:
        return {"ok": False, "message": f"Login failed: {e}"}
    finally:
        try:
            conn.logout()
        except Exception:
            try:
                conn.shutdown()
            except Exception:
                pass
