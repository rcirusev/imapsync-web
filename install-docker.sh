#!/usr/bin/env bash
#
# Docker installer + launcher for imapsync Web UI.
#
# Checks whether Docker (Engine + the `docker compose` plugin) is already
# on this machine; if not, installs it using Docker's own official install
# script (https://get.docker.com), which covers Debian/Ubuntu and
# RHEL/Fedora-family distros alike — same distro families install.sh
# supports for the native path. Then brings the stack up the same way the
# manual steps in README.md's "Docker" section do (`docker compose up -d`),
# so a brand-new server needs exactly one command instead of "is Docker
# even installed?" being step zero.
#
# Safe to re-run: if Docker is already installed it skips straight to
# `docker compose up -d`, same as install.sh updates the systemd install
# in place.
#
# Usage:
#   sudo ./install-docker.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '\033[1;34m[docker]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[docker]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[docker]\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this with sudo / as root (it may need to install Docker packages, and always needs to run docker compose)."

# ---------------------------------------------------------------------------
# 1. Docker Engine + the compose plugin
# ---------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
  log "Docker already installed: $(command -v docker) ($(docker --version))"
else
  log "Docker not found — installing via Docker's official install script (get.docker.com)…"

  # Docker's script needs curl (or wget); make sure one exists first.
  if ! command -v curl >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      apt-get update -y && apt-get install -y curl
    elif command -v dnf >/dev/null 2>&1; then
      dnf install -y curl
    elif command -v yum >/dev/null 2>&1; then
      yum install -y curl
    else
      die "curl is required to fetch Docker's install script — install curl manually then re-run."
    fi
  fi

  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh

  systemctl enable --now docker >/dev/null 2>&1 || service docker start || true

  command -v docker >/dev/null 2>&1 || die "Docker install failed — install it manually (see https://docs.docker.com/engine/install/) then re-run this script."
  log "Docker installed: $(docker --version)"

  # Convenience for later, non-root use — doesn't affect this run since
  # group membership only takes effect in new shells.
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    usermod -aG docker "$SUDO_USER" || true
    log "Added '$SUDO_USER' to the 'docker' group — log out/in (or run 'newgrp docker') to run docker without sudo from now on."
  fi
fi

docker compose version >/dev/null 2>&1 || die "Docker was found but the 'docker compose' plugin isn't available — see https://docs.docker.com/compose/install/"

# ---------------------------------------------------------------------------
# 2. .env (only on first run — never overwrite one that already exists)
# ---------------------------------------------------------------------------
cd "$SCRIPT_DIR"
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  log "Created .env from .env.example — edit it to set IMAPSYNC_WEB_USERNAME/PASSWORD, then re-run this script (or 'docker compose up -d') to apply it."
fi

# ---------------------------------------------------------------------------
# 3. Bring the stack up
# ---------------------------------------------------------------------------
log "Pulling the image and starting the stack…"
docker compose pull
docker compose up -d

sleep 1
if docker compose ps --status running --services 2>/dev/null | grep -q .; then
  log "Container is running."
else
  warn "Container did not come up cleanly — check: docker compose logs -f"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PORT="$(grep -m1 '^PORT=' .env 2>/dev/null | cut -d= -f2)"
PORT="${PORT:-8000}"
cat <<EOF

──────────────────────────────────────────────────────────────────
 imapsync Web UI is running in Docker.

 URL (local):   http://127.0.0.1:${PORT}
 URL (network): http://${IP:-<server-ip>}:${PORT}

 Manage it with:
   docker compose ps
   docker compose logs -f
   docker compose restart
   docker compose pull && docker compose up -d   # upgrade

 SECURITY: don't expose port ${PORT} directly to the internet — put it
 behind a reverse proxy with HTTPS and authentication, and restrict who
 can reach it with a firewall (see README.md's Security notes).
──────────────────────────────────────────────────────────────────
EOF
