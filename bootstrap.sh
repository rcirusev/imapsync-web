#!/usr/bin/env bash
#
# One-command bootstrap for a brand-new server: installs git and Docker if
# either is missing, clones (or updates) imapsync-web, and brings up the
# pre-built image from GHCR. This is the entry point for "just spun up a
# fresh VM for this client's migration" — a machine that has nothing on it
# yet, not even git.
#
# If you already have the repo cloned (e.g. a dev machine, or updating an
# existing install), use `sudo ./install-docker.sh` from inside it instead
# — same Docker check + `docker compose up -d`, without re-cloning into a
# new directory.
#
# Usage (from a brand-new server):
#   curl -fsSL https://raw.githubusercontent.com/rcirusev/imapsync-web/main/bootstrap.sh | sudo bash
#
# Prefer to read it before running it as root (recommended for anything
# piped into `sudo bash`)?
#   curl -fsSLO https://raw.githubusercontent.com/rcirusev/imapsync-web/main/bootstrap.sh
#   less bootstrap.sh
#   sudo bash bootstrap.sh
#
# Override the install location (default /opt/imapsync-web):
#   curl -fsSL .../bootstrap.sh | sudo INSTALL_DIR=/srv/imapsync-web bash
#
# Safe to re-run: an existing $INSTALL_DIR gets `git pull` instead of a
# fresh clone, and Docker/git steps are skipped once already installed —
# so this also doubles as the update command later.
#
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/imapsync-web}"
REPO_URL="${REPO_URL:-https://github.com/rcirusev/imapsync-web.git}"

log()  { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[bootstrap]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[bootstrap]\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this with sudo / as root (it installs system packages)."

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
# 1. git
# ---------------------------------------------------------------------------
if command -v git >/dev/null 2>&1; then
  log "git already installed: $(command -v git)"
else
  log "git not found — installing…"
  case "$PKG_MGR" in
    apt) apt-get update -y && apt-get install -y git ;;
    dnf|yum) $PKG_MGR install -y git ;;
  esac
  command -v git >/dev/null 2>&1 || die "git install failed — install it manually then re-run."
fi

# ---------------------------------------------------------------------------
# 2. Docker Engine + the compose plugin
# ---------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
  log "Docker already installed: $(command -v docker) ($(docker --version))"
else
  log "Docker not found — installing via Docker's official install script (get.docker.com)…"

  if ! command -v curl >/dev/null 2>&1; then
    case "$PKG_MGR" in
      apt) apt-get update -y && apt-get install -y curl ;;
      dnf|yum) $PKG_MGR install -y curl ;;
    esac
  fi

  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh

  systemctl enable --now docker >/dev/null 2>&1 || service docker start || true

  command -v docker >/dev/null 2>&1 || die "Docker install failed — install it manually (see https://docs.docker.com/engine/install/) then re-run this script."
  log "Docker installed: $(docker --version)"

  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    usermod -aG docker "$SUDO_USER" || true
    log "Added '$SUDO_USER' to the 'docker' group — log out/in (or run 'newgrp docker') to run docker without sudo from now on."
  fi
fi

docker compose version >/dev/null 2>&1 || die "Docker was found but the 'docker compose' plugin isn't available — see https://docs.docker.com/compose/install/"

# ---------------------------------------------------------------------------
# 3. Get the code
# ---------------------------------------------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
  log "Repo already present at $INSTALL_DIR — pulling latest…"
  git -C "$INSTALL_DIR" pull
else
  log "Cloning imapsync-web into $INSTALL_DIR…"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# 4. .env + bring the stack up — pulls the pre-built image published to
#    GHCR by this repo's own docker-publish.yml, no local build needed.
# ---------------------------------------------------------------------------
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  log "Created .env from .env.example — edit it to set IMAPSYNC_WEB_USERNAME/PASSWORD, then re-run this script (or 'docker compose up -d' from $INSTALL_DIR) to apply it."
fi

log "Pulling the image and starting the stack…"
docker compose pull
docker compose up -d

sleep 1
if docker compose ps --status running --services 2>/dev/null | grep -q .; then
  log "Container is running."
else
  warn "Container did not come up cleanly — check: cd $INSTALL_DIR && docker compose logs -f"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PORT="$(grep -m1 '^PORT=' .env 2>/dev/null | cut -d= -f2)"
PORT="${PORT:-8000}"
cat <<EOF

──────────────────────────────────────────────────────────────────
 imapsync Web UI installed in ${INSTALL_DIR} and running in Docker.

 URL (local):   http://127.0.0.1:${PORT}
 URL (network): http://${IP:-<server-ip>}:${PORT}

 Manage it with (from ${INSTALL_DIR}):
   docker compose ps
   docker compose logs -f
   docker compose restart
   docker compose pull && docker compose up -d   # upgrade

 SECURITY: don't expose port ${PORT} directly to the internet — put it
 behind a reverse proxy with HTTPS and authentication, and restrict who
 can reach it with a firewall (see README.md's Security notes).
──────────────────────────────────────────────────────────────────
EOF
