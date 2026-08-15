#!/bin/bash
# install.sh — first-time production installer for ClickHouse Console v4 (bare-metal).
#
# Use this ONCE, for a fresh install. For later code updates use deploy/update.sh
# instead — that script syncs new code into an EXISTING install and restarts the
# service. This script sets the install up from nothing.
#
# Turns a manually-run, root-owned install into a proper service:
#   - dedicated unprivileged user `chconsole`
#   - install root /opt/clickhouse-console with a virtualenv
#   - gunicorn under systemd (auto-start on boot, auto-restart on crash)
#   - nginx TLS reverse proxy with a self-signed certificate
#
# It is IDEMPOTENT and NON-DESTRUCTIVE to your state:
#   - an existing /opt/clickhouse-console/.env is kept as-is
#   - an existing /opt/clickhouse-console/data/ is kept as-is
#   - it does NOT start the service — you review .env, then start it
#     yourself (the exact commands are printed at the end).
#
# Run as root, FROM the unzipped release directory:
#   sudo bash deploy/install.sh
#
# Audit hardening (harden.sql) is a Postgres-side step and is NOT run here
# — see the printed instructions at the end.

set -euo pipefail

APP_USER="chconsole"
APP_GROUP="chconsole"
INSTALL_ROOT="/opt/clickhouse-console"
LOG_DIR="/var/log/clickhouse-console"
CERT_DIR="/etc/ssl/clickhouse-console"
SERVICE_SRC="deploy/clickhouse-console.service"
NGINX_SITE_SRC="nginx/clickhouse-console.site"
NGINX_SITE_DST="/etc/nginx/sites-available/clickhouse-console"

say()  { echo -e "→ $*"; }
warn() { echo -e "⚠ $*" >&2; }
die()  { echo -e "✗ $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo bash deploy/install.sh)."
[ -f "$SERVICE_SRC" ] || die "Run this from the unzipped release dir (deploy/ not found)."
[ -f app.py ] || die "app.py not found — wrong directory?"

RELEASE_DIR="$(pwd)"

# ── 1. Service user ───────────────────────────────────────────────────
if id "$APP_USER" >/dev/null 2>&1; then
    say "User '$APP_USER' already exists — keeping it."
else
    say "Creating system user '$APP_USER' (no login shell)."
    useradd --system --home-dir "$INSTALL_ROOT" --shell /usr/sbin/nologin "$APP_USER"
fi

# ── 2. Install root + copy release ────────────────────────────────────
say "Syncing release to $INSTALL_ROOT (preserving existing .env and data/)."
mkdir -p "$INSTALL_ROOT"

# Copy code, but never clobber the operator's .env or data/ or certs.
EXCLUDES=(--exclude='.env' --exclude='data/' --exclude='.venv/' --exclude='nginx/certs/')
if command -v rsync >/dev/null 2>&1; then
    rsync -a "${EXCLUDES[@]}" "$RELEASE_DIR"/ "$INSTALL_ROOT"/
else
    # Fallback without rsync: tar-pipe with the same exclusions.
    tar -C "$RELEASE_DIR" \
        --exclude='./.env' --exclude='./data' --exclude='./.venv' --exclude='./nginx/certs' \
        -cf - . | tar -C "$INSTALL_ROOT" -xf -
fi

mkdir -p "$INSTALL_ROOT/data" "$INSTALL_ROOT/logs" "$LOG_DIR"

# ── 3. .env ───────────────────────────────────────────────────────────
if [ -f "$INSTALL_ROOT/.env" ]; then
    say ".env already present at $INSTALL_ROOT/.env — keeping it."
elif [ -f "$RELEASE_DIR/.env" ]; then
    say "Copying .env from the release directory."
    cp "$RELEASE_DIR/.env" "$INSTALL_ROOT/.env"
else
    warn "No .env found. Writing a TEMPLATE to $INSTALL_ROOT/.env — you MUST edit it."
    cat > "$INSTALL_ROOT/.env" <<'ENVEOF'
# ── PostgreSQL ────────────────────────────────────────────
DB_HOST=192.168.105.2
DB_PORT=5432
DB_USER=chconsole
DB_PASSWORD=CHANGE_ME
DB_NAME=chconsole
DB_POOL_MIN=2
DB_POOL_MAX=20
# ── Redis (Phase 2: session store) ────────────────────────
REDIS_HOST=192.168.105.4
REDIS_PORT=6379
REDIS_PASSWORD=CHANGE_ME
REDIS_DB=0
# ── Console secrets ───────────────────────────────────────
MASTER_KEY=CHANGE_ME
SESSION_TTL_DAYS=7
TZ=Europe/Amsterdam
# ── gunicorn (Phase 3) ────────────────────────────────────
GUNICORN_WORKERS=2
GUNICORN_THREADS=4
ENVEOF
fi
chmod 600 "$INSTALL_ROOT/.env"

# ── 4. Virtualenv + dependencies ──────────────────────────────────────
if [ ! -d "$INSTALL_ROOT/.venv" ]; then
    say "Creating virtualenv."
    python3 -m venv "$INSTALL_ROOT/.venv"
fi
say "Installing/updating Python dependencies (incl. gunicorn, redis, psycopg)."
"$INSTALL_ROOT/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_ROOT/.venv/bin/pip" install --quiet -r "$INSTALL_ROOT/requirements.txt"

# ── 5. Ownership ──────────────────────────────────────────────────────
say "Setting ownership to $APP_USER."
chown -R "$APP_USER:$APP_GROUP" "$INSTALL_ROOT" "$LOG_DIR"
chmod +x "$INSTALL_ROOT/run-prod.sh" || true

# ── 6. systemd unit ───────────────────────────────────────────────────
say "Installing systemd unit."
cp "$SERVICE_SRC" /etc/systemd/system/clickhouse-console.service
systemctl daemon-reload
systemctl enable clickhouse-console >/dev/null 2>&1 || true

# ── 7. nginx site + self-signed certificate ───────────────────────────
if [ ! -f "$CERT_DIR/server.crt" ]; then
    say "Generating self-signed TLS certificate (365 days, CN=clickhouse-console)."
    mkdir -p "$CERT_DIR"
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout "$CERT_DIR/server.key" \
        -out "$CERT_DIR/server.crt" \
        -subj "/CN=clickhouse-console" >/dev/null 2>&1
    chmod 600 "$CERT_DIR/server.key"
else
    say "TLS certificate already exists at $CERT_DIR — keeping it."
fi

if [ -d /etc/nginx/sites-available ]; then
    say "Installing nginx site config."
    cp "$NGINX_SITE_SRC" "$NGINX_SITE_DST"
    ln -sf "$NGINX_SITE_DST" /etc/nginx/sites-enabled/clickhouse-console
    if nginx -t >/dev/null 2>&1; then
        say "nginx config valid."
    else
        warn "nginx -t reported a problem — review before reloading nginx."
    fi
else
    warn "/etc/nginx/sites-available not found — install nginx, then copy"
    warn "  $NGINX_SITE_SRC manually. See INSTALLATION.md §5.7."
fi

# ── Done ──────────────────────────────────────────────────────────────
cat <<DONE

────────────────────────────────────────────────────────────────────
✓ Phase 3 install staged. The service is NOT started yet — review first.

Next steps:

  1. Review / edit the environment file:
       sudo nano $INSTALL_ROOT/.env
     (DB_*, REDIS_*, MASTER_KEY must be correct. Bring these from your
      previous ~/v2_full/.env if this is an upgrade.)

  2. Start the service:
       sudo systemctl start clickhouse-console
       sudo systemctl status clickhouse-console
       journalctl -u clickhouse-console -f      # live logs

  3. Reload nginx to serve TLS:
       sudo systemctl reload nginx

  4. Audit hardening (Postgres side, run ONCE, as a Postgres superuser):
       psql -h <db-host> -U postgres -d chconsole -f $INSTALL_ROOT/harden.sql

  5. Open  https://<this-host>/  — the self-signed cert will show a
     browser warning; that is expected for an internal certificate.

Your old manual install under ~/v2_full is untouched. Once the systemd
service is confirmed working, you can retire it.
────────────────────────────────────────────────────────────────────
DONE
