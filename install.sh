#!/usr/bin/env bash
#
# Universal installer for imapsync Web UI.
#
# Installs imapsync itself (if missing), sets up a Python venv, copies this
# project to a persistent location, and registers it as a systemd service
# that survives reboots and restarts on failure. Safe to re-run: it will
# update the install in place and restart the service.
#
# Usage:
#   sudo ./install.sh                     # install to /opt/imapsync-web, port 8000
#                                          # (will interactively ask whether to
#                                          # set up a login for the web UI)
#   sudo INSTALL_DIR=/srv/imapsync-web PORT=9000 ./install.sh
#   sudo IMAPSYNC_WEB_USERNAME=admin IMAPSYNC_WEB_PASSWORD=... ./install.sh
#                                          # turns on HTTP Basic Auth non-interactively
#                                          # (skips the prompt; use single quotes around
#                                          # the password if it has special characters)
#
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/imapsync-web}"
SERVICE_USER="${SERVICE_USER:-imapsyncweb}"
PORT="${PORT:-8000}"
IMAPSYNC_WEB_USERNAME="${IMAPSYNC_WEB_USERNAME:-}"
IMAPSYNC_WEB_PASSWORD="${IMAPSYNC_WEB_PASSWORD:-}"
SERVICE_NAME="imapsync-web"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[install]\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this with sudo / as root (it installs system packages and a systemd service)."

# ---------------------------------------------------------------------------
# Interactive login setup (only if not already provided via env vars, and
# only when the script is run from an actual terminal — piping the script
# through a non-interactive shell just skips the prompt, no error).
# Asking interactively means the password never has to be typed on the
# command line, so shell metacharacters in it (&, #, $, etc.) are never a
# problem — unlike passing IMAPSYNC_WEB_PASSWORD=... directly.
# ---------------------------------------------------------------------------
if [ -z "$IMAPSYNC_WEB_USERNAME" ] && [ -z "$IMAPSYNC_WEB_PASSWORD" ]; then
  if [ -t 0 ]; then
    echo
    read -r -p "$(printf '\033[1;34m[install]\033[0m Set up a login (HTTP Basic Auth) for the web UI? [y/N]: ')" ENABLE_AUTH_ANSWER
    if [[ "$ENABLE_AUTH_ANSWER" =~ ^[Yy]$ ]]; then
      while true; do
        read -r -p "  Username: " IMAPSYNC_WEB_USERNAME
        read -r -s -p "  Password: " IMAPSYNC_WEB_PASSWORD
        echo
        read -r -s -p "  Confirm password: " IMAPSYNC_WEB_PASSWORD_CONFIRM
        echo
        if [ -z "$IMAPSYNC_WEB_USERNAME" ] || [ -z "$IMAPSYNC_WEB_PASSWORD" ]; then
          warn "Username and password can't be empty — try again."
          continue
        fi
        if [ "$IMAPSYNC_WEB_PASSWORD" != "$IMAPSYNC_WEB_PASSWORD_CONFIRM" ]; then
          warn "Passwords didn't match — try again."
          continue
        fi
        unset IMAPSYNC_WEB_PASSWORD_CONFIRM
        break
      done
      log "Login will be enabled for user '${IMAPSYNC_WEB_USERNAME}'."
    else
      log "Skipping login setup — the web UI will be open to anyone who can reach it. You can enable this later by re-running with IMAPSYNC_WEB_USERNAME/IMAPSYNC_WEB_PASSWORD set."
    fi
  else
    warn "Not running interactively — skipping the login prompt. Set IMAPSYNC_WEB_USERNAME/IMAPSYNC_WEB_PASSWORD env vars beforehand to enable Basic Auth non-interactively."
  fi
fi

# ---------------------------------------------------------------------------
# 1. Detect package manager
# ---------------------------------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
  PKG_MGR=apt
elif command -v dnf >/dev/null 2>&1; then
  PKG_MGR=dnf
elif command -v yum >/dev/null 2>&1; then
  PKG_MGR=yum
else
  die "No supported package manager found (need apt-get, dnf, or yum)."
fi
log "Detected package manager: $PKG_MGR"

# ---------------------------------------------------------------------------
# 2. Install imapsync itself, if not already on PATH
# ---------------------------------------------------------------------------
if command -v imapsync >/dev/null 2>&1; then
  log "imapsync already installed: $(command -v imapsync)"
else
  log "imapsync not found — installing…"
  case "$PKG_MGR" in
    apt)
      apt-get update -y
      if apt-get install -y imapsync; then
        log "Installed imapsync from the distro repository."
      else
        # imapsync is not in current Debian/Ubuntu archives (verified against
        # Ubuntu 24.04) — build from source using the exact dependency list
        # from the project's own INSTALL.Ubuntu.txt / INSTALL.Debian.txt.
        warn "imapsync package not available from apt on this release; building from source."
        apt-get install -y \
          libauthen-ntlm-perl libclass-load-perl libcrypt-openssl-rsa-perl \
          libcrypt-ssleay-perl libdata-uniqid-perl libdigest-hmac-perl \
          libdist-checkconflicts-perl libencode-imaputf7-perl \
          libfile-copy-recursive-perl libfile-tail-perl libio-compress-perl \
          libio-socket-inet6-perl libio-socket-ssl-perl libio-tee-perl \
          libjson-webtoken-perl libmail-imapclient-perl libmodule-scandeps-perl \
          libnet-dbus-perl libnet-dns-perl libnet-ssleay-perl libpar-packer-perl \
          libproc-processtable-perl libreadonly-perl libregexp-common-perl \
          libsys-meminfo-perl libterm-readkey-perl libtest-fatal-perl \
          libtest-mock-guard-perl libtest-mockobject-perl libtest-pod-perl \
          libtest-requires-perl libtest-simple-perl libunicode-string-perl \
          liburi-perl libtest-nowarnings-perl libtest-deep-perl libtest-warn-perl \
          make time cpanminus git perl
        git clone --depth 1 https://github.com/imapsync/imapsync.git /opt/imapsync-src
        perl -c /opt/imapsync-src/imapsync
        cp /opt/imapsync-src/imapsync /usr/local/bin/imapsync
        chmod +x /usr/local/bin/imapsync
      fi
      ;;
    dnf|yum)
      $PKG_MGR install -y epel-release || true
      if $PKG_MGR install -y imapsync; then
        log "Installed imapsync via $PKG_MGR."
      else
        warn "imapsync package not available via $PKG_MGR on this release; building from source."
        $PKG_MGR install -y git perl perl-CPAN gcc make \
          perl-Mail-IMAPClient perl-IO-Socket-SSL perl-Term-ReadKey \
          perl-JSON-WebToken perl-Unicode-String perl-Authen-NTLM \
          perl-File-Copy-Recursive perl-Readonly perl-Sys-MemInfo || true
        git clone --depth 1 https://github.com/imapsync/imapsync.git /opt/imapsync-src
        # RPM-family module names/coverage vary a lot by release; cpanm fills gaps.
        cpanm --notest --local-lib=/opt/imapsync-cpanm Mail::IMAPClient Encode::IMAPUTF7 \
          File::Copy::Recursive JSON::WebToken Unicode::String Authen::NTLM 2>&1 | tail -20 || true
        perl -c /opt/imapsync-src/imapsync || warn "imapsync still has unresolved Perl dependencies — see output above."
        cp /opt/imapsync-src/imapsync /usr/local/bin/imapsync
        chmod +x /usr/local/bin/imapsync
      fi
      ;;
  esac
  command -v imapsync >/dev/null 2>&1 || die "imapsync install failed — install it manually (see https://github.com/imapsync/imapsync) then re-run this script."
  log "imapsync ready: $(command -v imapsync)"
fi

# ---------------------------------------------------------------------------
# 3. Python + venv
# ---------------------------------------------------------------------------
log "Ensuring Python 3 + venv are installed…"
case "$PKG_MGR" in
  apt) apt-get install -y python3 python3-venv python3-pip ;;
  dnf|yum) $PKG_MGR install -y python3 python3-pip ;;
esac

# ---------------------------------------------------------------------------
# 4. Service user
# ---------------------------------------------------------------------------
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  log "Creating system user '$SERVICE_USER'…"
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# ---------------------------------------------------------------------------
# 5. Copy project into place
# ---------------------------------------------------------------------------
log "Installing app to $INSTALL_DIR…"
mkdir -p "$INSTALL_DIR"
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
  cp -r "$SCRIPT_DIR"/. "$INSTALL_DIR"/
fi
mkdir -p "$INSTALL_DIR/data/logs"

# ---------------------------------------------------------------------------
# 6. Python virtualenv + dependencies
# ---------------------------------------------------------------------------
log "Creating virtualenv and installing Python dependencies…"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# 7. systemd service
# ---------------------------------------------------------------------------
log "Writing systemd unit /etc/systemd/system/${SERVICE_NAME}.service…"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=imapsync Web UI
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=IMAPSYNC_WEB_DATA_DIR=${INSTALL_DIR}/data
$( [ -n "${IMAPSYNC_WEB_USERNAME}" ] && [ -n "${IMAPSYNC_WEB_PASSWORD}" ] && printf 'Environment=IMAPSYNC_WEB_USERNAME=%s\nEnvironment=IMAPSYNC_WEB_PASSWORD=%s' "${IMAPSYNC_WEB_USERNAME}" "${IMAPSYNC_WEB_PASSWORD}" )
ExecStart=${INSTALL_DIR}/.venv/bin/gunicorn -b 0.0.0.0:${PORT} --worker-class gthread --workers 2 --threads 8 --timeout 0 app:app
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

# The unit file may now contain a plaintext password — root-only, unlike
# systemd's usual world-readable default for unit files.
chmod 600 "/etc/systemd/system/${SERVICE_NAME}.service"

if [ -n "${IMAPSYNC_WEB_USERNAME}" ] && [ -n "${IMAPSYNC_WEB_PASSWORD}" ]; then
  log "HTTP Basic Auth enabled for user '${IMAPSYNC_WEB_USERNAME}'."
elif [ -n "${IMAPSYNC_WEB_USERNAME}" ] || [ -n "${IMAPSYNC_WEB_PASSWORD}" ]; then
  warn "Only one of IMAPSYNC_WEB_USERNAME/IMAPSYNC_WEB_PASSWORD was set — both are required to turn on Basic Auth. Leaving the app open."
fi

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

sleep 1
if systemctl is-active --quiet "${SERVICE_NAME}"; then
  log "Service is running."
else
  warn "Service did not start — check: journalctl -u ${SERVICE_NAME} -e"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

──────────────────────────────────────────────────────────────────
 imapsync Web UI installed.

 URL (local):   http://127.0.0.1:${PORT}
 URL (network): http://${IP:-<server-ip>}:${PORT}

 Manage it with:
   systemctl status ${SERVICE_NAME}
   systemctl restart ${SERVICE_NAME}
   journalctl -u ${SERVICE_NAME} -f

 Data (SQLite history + per-job logs): ${INSTALL_DIR}/data

 SECURITY: this app now handles real IMAP passwords and runs a real
 migration tool. Do not expose port ${PORT} directly to the internet —
 put it behind a reverse proxy with HTTPS and authentication (nginx +
 Let's Encrypt + basic auth, or a VPN/SSH tunnel), and restrict who can
 reach it with a firewall.
──────────────────────────────────────────────────────────────────
EOF
