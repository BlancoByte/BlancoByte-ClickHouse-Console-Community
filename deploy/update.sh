#!/usr/bin/env bash
# deploy/update.sh — one-command UPDATE for ClickHouse Console v4.
#
# This script ONLY updates an existing install. For a first-time install:
#   sudo bash deploy/install.sh
#
# USAGE
#   sudo bash deploy/update.sh                   # update /opt from THIS unzipped release dir
#   sudo bash deploy/update.sh /root/release.zip # unzip the given ZIP to a temp dir, update /opt from it
#   sudo bash deploy/update.sh --no-restart      # sync code but do NOT restart the service
#
# WHAT IT DOES
#   1. Works out the release directory. If a ZIP is given as an argument it is
#      unzipped to a temp dir (mktemp); that temp dir is removed when done.
#   2. rsyncs the code into /opt/clickhouse-console. .env, data/, logs/, .venv/
#      and nginx/certs/ are NEVER touched (excluded from the sync). A short,
#      explicit list of obsolete files is removed by name — there is NO blanket
#      rsync --delete, which would also wipe per-install files (license, certs).
#      Fixes file ownership, refreshes Python dependencies, restarts the service.
#   3. If /opt/clickhouse-console does not exist it ERRORS OUT — run
#      deploy/install.sh first.
#
# It never touches your secrets or data. It does NOT delete your own
# /root/v2_full directory — only the temp dir it created itself.
set -euo pipefail

INSTALL_ROOT="/opt/clickhouse-console"
SERVICE="clickhouse-console"
SERVICE_USER="chconsole"
DO_RESTART=1
ZIP_ARG=""
TMP_DIR=""

# ── small helpers ───────────────────────────────────────────────────────
c_grn=$'\e[32m'; c_red=$'\e[31m'; c_yel=$'\e[33m'; c_rst=$'\e[0m'
say()  { echo "${c_grn}==>${c_rst} $*"; }
warn() { echo "${c_yel}warning:${c_rst} $*"; }
die()  { echo "${c_red}error:${c_rst} $*" >&2; exit 1; }
cleanup() { [ -n "$TMP_DIR" ] && rm -rf "$TMP_DIR"; return 0; }
trap cleanup EXIT

usage() {
  cat <<'HELPTEXT'
ClickHouse Console v4 — one-command UPDATE
(For a first-time install: sudo bash deploy/install.sh)

USAGE
  sudo bash deploy/update.sh                   update /opt from this unzipped release dir
  sudo bash deploy/update.sh /root/release.zip unzip the given ZIP to a temp dir, update /opt from it
  sudo bash deploy/update.sh --no-restart      sync code but do not restart the service

WHAT IT DOES
  - rsyncs the code into /opt/clickhouse-console, fixes ownership, refreshes
    Python dependencies, restarts the service.
  - .env, data/, logs/, .venv/, nginx/certs/ are NEVER touched (excluded).
  - A short, explicit list of obsolete files is removed by name. There is NO
    rsync --delete — per-install files (license, certs) are never at risk.
  - If a ZIP argument is given, the temp dir it is unzipped to is removed when done.
  - Errors out if /opt/clickhouse-console does not exist — run deploy/install.sh first.
HELPTEXT
}

# ── arguments ───────────────────────────────────────────────────────────
for a in "$@"; do
  case "$a" in
    --no-restart) DO_RESTART=0 ;;
    -h|--help) usage; exit 0 ;;
    *.zip) ZIP_ARG="$a" ;;
    *) die "unknown argument: $a  (use --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "must run as root — use 'sudo bash $0 ...'."

# ── work out the release directory ──────────────────────────────────────
if [ -n "$ZIP_ARG" ]; then
  [ -f "$ZIP_ARG" ] || die "ZIP not found: $ZIP_ARG"
  command -v unzip >/dev/null 2>&1 || die "'unzip' is not installed — install it with 'apt install -y unzip'."
  TMP_DIR="$(mktemp -d)"
  say "Unzipping release → $TMP_DIR"
  unzip -q -o "$ZIP_ARG" -d "$TMP_DIR"
  if [ -d "$TMP_DIR/v2_full" ]; then
    RELEASE_DIR="$TMP_DIR/v2_full"            # standard top-level dir inside the ZIP
  else
    RELEASE_DIR="$TMP_DIR"
  fi
else
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  RELEASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)" # parent of the deploy/ directory
fi

[ -f "$RELEASE_DIR/app.py" ] || die "not a valid release directory (no app.py): $RELEASE_DIR"
say "Release directory: $RELEASE_DIR"

# ── check an install exists ─────────────────────────────────────────────
# This script ONLY updates. For a first-time install use deploy/install.sh.
if [ ! -d "$INSTALL_ROOT" ] || [ ! -f "/etc/systemd/system/${SERVICE}.service" ]; then
  die "$INSTALL_ROOT not found — this script only UPDATES an existing install.
       For a first-time install:  sudo bash deploy/install.sh"
fi

# ── update path ─────────────────────────────────────────────────────────
say "Updating existing install: $INSTALL_ROOT"

# Code sync — NEVER touch secrets / data / logs / venv / certificates.
EXCLUDES=(
  --exclude='.env'
  --exclude='data/'
  --exclude='logs/'
  --exclude='.venv/'
  --exclude='nginx/certs/'
  --exclude='__pycache__/'
  --exclude='*.pyc'
)
if command -v rsync >/dev/null 2>&1; then
  # Plain sync, NO --delete. --delete is intentionally NOT used: the install
  # directory legitimately holds per-install files that are not in the repo
  # (the license file, generated certs, ...), and a blanket --delete would
  # wipe them. Obsolete files are instead removed by explicit name below.
  rsync -a "${EXCLUDES[@]}" "$RELEASE_DIR"/ "$INSTALL_ROOT"/
else
  warn "rsync not found — copying with cp (.env / data/ / logs/ / .venv/ preserved)."
  ( cd "$RELEASE_DIR"
    find . -mindepth 1 -maxdepth 1 \
      ! -name '.env' ! -name 'data' ! -name 'logs' ! -name '.venv' \
      -exec cp -af {} "$INSTALL_ROOT"/ \; )
fi

# Remove files that earlier releases shipped but the current one no longer
# does. This is an EXPLICIT, hand-maintained list — deliberately NOT a blanket
# rsync --delete, which would also destroy per-install files (the license,
# generated certs, ...) that legitimately live here but are not in the repo.
OBSOLETE=( "NEXT_STEPS.md" "NEXT_STEPS-phase3.md" )
for _f in "${OBSOLETE[@]}"; do
  if [ -e "$INSTALL_ROOT/$_f" ]; then
    rm -f "$INSTALL_ROOT/$_f"
    say "Removed obsolete file: $_f"
  fi
done
say "Code synced."

# Ownership — the service runs as $SERVICE_USER.
chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_ROOT"

# Python dependencies — idempotent; fast when nothing changed.
if [ -x "$INSTALL_ROOT/.venv/bin/pip" ]; then
  say "Checking Python dependencies..."
  sudo -u "$SERVICE_USER" "$INSTALL_ROOT/.venv/bin/pip" install -q \
       -r "$INSTALL_ROOT/requirements.txt" \
    || warn "pip reported a warning — check journalctl if needed."
else
  warn ".venv not found — you may need to create the virtualenv with deploy/install.sh."
fi

# Database migrations — bring the schema to head BEFORE restarting the new code.
# Runs as root with the install's .env sourced (the migration only needs the DB
# credentials; it connects to Postgres and writes nothing to disk). If a
# migration fails we abort here and do NOT restart, so the running old code is
# never left pointed at a half-migrated schema.
if [ -x "$INSTALL_ROOT/.venv/bin/alembic" ]; then
  say "Applying database migrations (alembic upgrade head)..."
  (
    cd "$INSTALL_ROOT"
    set -a; [ -f .env ] && . ./.env; set +a
    "$INSTALL_ROOT/.venv/bin/alembic" upgrade head
  ) || die "alembic upgrade failed — schema NOT migrated; aborting before restart.
       The old service is still running on the previous code. Fix the error
       above (check DB connectivity and .env), then re-run this script."
  say "Migrations applied."
else
  warn "alembic not found in venv — skipping migrations (run deploy/install.sh or pip install -r requirements.txt)."
fi

# Restart so the new code actually takes effect.
if [ "$DO_RESTART" -eq 1 ]; then
  say "Restarting service: $SERVICE"
  systemctl restart "$SERVICE"
  sleep 1
  if systemctl is-active --quiet "$SERVICE"; then
    say "Service is running. OK"
  else
    die "Service did not start! Check the logs: journalctl -u $SERVICE -n 50 --no-pager"
  fi
else
  warn "--no-restart given — the new code takes effect only after 'sudo systemctl restart $SERVICE'."
fi

echo
say "Update complete."
echo "  Health:  curl -sk https://127.0.0.1/health"
echo "  Logs:    journalctl -u $SERVICE -f"
