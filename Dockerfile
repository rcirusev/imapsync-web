# imapsync Web UI — container image
#
# Bundles the real `imapsync` Perl binary (built from source, same
# dependency list and steps install.sh uses on Debian/Ubuntu, verified
# against Ubuntu 24.04 — see install.sh's own comments) together with this
# Flask app, served by gunicorn. Runs as an unprivileged user; all
# persistent state (SQLite history, per-job logs, the delta-sync secret
# key) lives under /data — mount a volume there so it survives container
# restarts/upgrades.
#
# Build:
#   docker build -t imapsync-web .
# Run (see also docker-compose.yml):
#   docker run -d --name imapsync-web -p 8000:8000 \
#     -v imapsync_data:/data \
#     -e IMAPSYNC_WEB_USERNAME=admin -e IMAPSYNC_WEB_PASSWORD='change-me' \
#     imapsync-web

# ---------------------------------------------------------------------------
# Stage 1: build imapsync itself from source (same package list + steps as
# install.sh's apt fallback path).
# ---------------------------------------------------------------------------
FROM ubuntu:24.04 AS imapsync-build
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      git perl make ca-certificates \
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
    && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 https://github.com/imapsync/imapsync.git /opt/imapsync-src \
    && perl -c /opt/imapsync-src/imapsync

# ---------------------------------------------------------------------------
# Stage 2: runtime image — the imapsync binary from stage 1, its runtime
# (non-build) Perl dependencies, Python + this app.
# ---------------------------------------------------------------------------
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv perl \
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
    && rm -rf /var/lib/apt/lists/*

COPY --from=imapsync-build /opt/imapsync-src/imapsync /usr/local/bin/imapsync
RUN chmod +x /usr/local/bin/imapsync

RUN useradd --system --create-home --home-dir /app --shell /usr/sbin/nologin imapsyncweb

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
ENV PATH="/opt/venv/bin:${PATH}"

COPY app.py bulk_csv.py conn_test.py crypto_store.py db.py imapsync_runner.py ./
COPY templates ./templates
COPY static ./static

RUN mkdir -p /data/logs && chown -R imapsyncweb:imapsyncweb /app /data

ENV IMAPSYNC_WEB_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000

USER imapsyncweb

CMD ["gunicorn", "-b", "0.0.0.0:8000", "--worker-class", "gthread", "--workers", "2", "--threads", "8", "--timeout", "0", "app:app"]
