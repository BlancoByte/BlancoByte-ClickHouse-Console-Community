#!/bin/bash
# run-prod.sh — production start (gunicorn).
#
# Phase 3: gunicorn is the default production server, replacing the Flask
# dev server (app.run). The systemd unit (deploy/clickhouse-console.service)
# calls this script, and it also works for a manual foreground start.
#
# Worker/thread counts are env-configurable so you can tune per VM size
# without editing this file:
#   GUNICORN_WORKERS  (default 2)   — processes; ~1 per core, keep RAM in mind
#   GUNICORN_THREADS  (default 4)   — threads per worker (gthread worker)
#   GUNICORN_BIND     (default 127.0.0.1:5000) — nginx proxies to this
#   GUNICORN_TIMEOUT  (default 120) — worker timeout in seconds
#
# Note: gunicorn imports app:app, which runs app.py's import-time
# init_db() / migrate_legacy_users() once per worker. Both are idempotent
# (schema uses IF NOT EXISTS; migrate returns early when users exist), so
# multiple workers are safe.

set -e
cd "$(dirname "$0")"

# Load .env for a manual start. Under systemd, EnvironmentFile= already
# provides these — sourcing again here is harmless (same values).
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# Activate the virtualenv if one exists (bare-metal install). If the deps
# are installed system-wide instead, this is skipped.
if [ -f .venv/bin/activate ]; then
    # shellcheck disable=SC1091
    . .venv/bin/activate
fi

WORKERS="${GUNICORN_WORKERS:-2}"
THREADS="${GUNICORN_THREADS:-4}"
BIND="${GUNICORN_BIND:-127.0.0.1:5000}"
TIMEOUT="${GUNICORN_TIMEOUT:-120}"

export PYTHONDONTWRITEBYTECODE=1

echo "Starting gunicorn: ${WORKERS} workers x ${THREADS} threads on ${BIND}"
exec gunicorn \
    -w "${WORKERS}" \
    -k gthread \
    --threads "${THREADS}" \
    -b "${BIND}" \
    --timeout "${TIMEOUT}" \
    --access-logfile - \
    --capture-output \
    app:app
