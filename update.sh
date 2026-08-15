#!/usr/bin/env bash
# update.sh — Safe in-place upgrade for ClickHouse-Console
#
# What it does:
#   1. Stops the running server
#   2. Snapshots your current install (full backup, kept for rollback)
#   3. Extracts new code from the zip
#   4. Replaces only code files — preserves data/, keys, .env
#   5. Validates the new code parses (Python syntax check)
#   6. Starts the server
#   7. Verifies the server is running; rolls back on failure
#
# What's preserved (NEVER overwritten):
#   data/                  — users, sessions, audit log, query history,
#                            connection registry, encrypted credentials,
#                            license file, instance fingerprint
#   public_key.pem         — license verification key
#   private_key.pem        — vendor only (if present, kept untouched)
#   .env                   — your environment configuration
#   nginx/certs/           — your TLS certificates
#
# Usage:
#   ./update.sh /path/to/clickhouse-console-X.Y.zip [/path/to/install]
#
# Examples:
#   # Update install in current directory (default)
#   cd /root && ./clickhouse-console/update.sh ~/Downloads/clickhouse-console-3.2.zip
#
#   # Update a specific install
#   ./update.sh ~/clickhouse-console-3.2.zip /opt/clickhouse-console
#
# Rollback (if anything goes wrong):
#   The script keeps a timestamped backup at <install>.bak.<timestamp>/
#   To roll back manually:
#     pkill -f "python.*app.py"
#     rm -rf /path/to/install
#     mv /path/to/install.bak.YYYYMMDD_HHMMSS /path/to/install
#     cd /path/to/install && python3 app.py

set -euo pipefail

# ─── Args ─────────────────────────────────────────────────────────────────
ZIP_PATH="${1:-}"
# Default install dir = parent dir of this script (script lives inside install)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${2:-$SCRIPT_DIR}"
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd)"

if [ -z "$ZIP_PATH" ]; then
    cat <<USAGE
Usage:
  $0 <new-zip-path> [install-dir]

Example:
  $0 ~/Downloads/clickhouse-console-3.2.zip
  $0 ~/Downloads/clickhouse-console-3.2.zip /opt/clickhouse-console
USAGE
    exit 2
fi

if [ ! -f "$ZIP_PATH" ]; then
    echo "ERROR: zip file not found: $ZIP_PATH" >&2
    exit 2
fi
ZIP_PATH="$(realpath "$ZIP_PATH")"

if [ ! -f "$INSTALL_DIR/app.py" ]; then
    echo "ERROR: $INSTALL_DIR does not look like a ClickHouse-Console install (no app.py)" >&2
    exit 2
fi

TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${INSTALL_DIR}.bak.${TS}"
STAGE_DIR="$(mktemp -d -t cc-update.XXXXXXXX)"
trap 'rm -rf "$STAGE_DIR"' EXIT

# Items NEVER overwritten on update — customer state.
PRESERVE=(
    data
    public_key.pem
    private_key.pem
    .env
    nginx/certs
    console.log
)

# ─── 0. Sanity ────────────────────────────────────────────────────────────
echo "============================================================"
echo "ClickHouse-Console — In-Place Update"
echo "============================================================"
echo "Source zip : $ZIP_PATH"
echo "Install dir: $INSTALL_DIR"
echo "Backup dir : $BACKUP_DIR"
echo "Staging    : $STAGE_DIR"
echo

# ─── 1. Stop server ───────────────────────────────────────────────────────
echo "[1/6] Stopping running server..."
SERVER_RUNNING=false
if pgrep -f "python.*app\.py" >/dev/null 2>&1; then
    SERVER_RUNNING=true
    pkill -f "python.*app\.py" 2>/dev/null || true
    sleep 2
    if pgrep -f "python.*app\.py" >/dev/null 2>&1; then
        pkill -9 -f "python.*app\.py" 2>/dev/null || true
        sleep 1
    fi
    echo "      Stopped"
else
    echo "      (server was not running)"
fi

# ─── 2. Snapshot current install ──────────────────────────────────────────
echo "[2/6] Backing up current install -> $BACKUP_DIR"
cp -a "$INSTALL_DIR" "$BACKUP_DIR"
echo "      Backup size: $(du -sh "$BACKUP_DIR" | awk '{print $1}')"

rollback() {
    echo
    echo "!! ROLLING BACK due to failure"
    if pgrep -f "python.*app\.py" >/dev/null 2>&1; then
        pkill -9 -f "python.*app\.py" 2>/dev/null || true
        sleep 1
    fi
    rm -rf "$INSTALL_DIR"
    mv "$BACKUP_DIR" "$INSTALL_DIR"
    if $SERVER_RUNNING; then
        cd "$INSTALL_DIR" && nohup python3 app.py >/dev/null 2>&1 &
        sleep 2
        echo "   Original install restored and server restarted."
    else
        echo "   Original install restored (server was not running before)."
    fi
    exit 1
}

# ─── 3. Extract new code into staging ─────────────────────────────────────
echo "[3/6] Extracting new code into staging area..."
unzip -q "$ZIP_PATH" -d "$STAGE_DIR" || { echo "ERROR: unzip failed"; rollback; }
NEW_SRC="$(find "$STAGE_DIR" -maxdepth 3 -name app.py -printf '%h\n' 2>/dev/null | head -1)"
if [ -z "$NEW_SRC" ] || [ ! -f "$NEW_SRC/app.py" ]; then
    echo "ERROR: could not find app.py inside the zip"
    rollback
fi
NEW_VERSION="$(grep -oE 'VERSION [0-9]+\.[0-9]+' "$NEW_SRC/static/index.html" 2>/dev/null | head -1 || echo 'unknown')"
echo "      Found new code at: $NEW_SRC ($NEW_VERSION)"

# ─── 4. Replace code files (preserve customer state) ──────────────────────
echo "[4/6] Replacing code files (data/ and keys preserved)..."
preserve_match() {
    local name="$1"
    for p in "${PRESERVE[@]}"; do
        # Match exact name OR top-level path component
        [ "$name" = "$p" ] && return 0
        [ "$name" = "${p%%/*}" ] && return 0
    done
    return 1
}

# Build full list of items in new src (top-level only)
for item in "$NEW_SRC"/* "$NEW_SRC"/.[!.]*; do
    [ -e "$item" ] || continue
    base="$(basename "$item")"
    if preserve_match "$base"; then
        echo "      skip: $base (preserved from existing install)"
        continue
    fi
    # Replace
    rm -rf "$INSTALL_DIR/$base"
    cp -a "$item" "$INSTALL_DIR/"
    echo "      updated: $base"
done

# Re-create the data/ directory if it doesn't exist (fresh install case shouldn't
# hit this, but defensive)
mkdir -p "$INSTALL_DIR/data"

# ─── 5. Validate new code parses ──────────────────────────────────────────
echo "[5/6] Validating new code..."
if ! python3 -c "import ast; ast.parse(open('$INSTALL_DIR/app.py').read())" 2>&1; then
    echo "ERROR: app.py syntax check failed"
    rollback
fi
# Quick import test (catches missing dependencies)
if ! ( cd "$INSTALL_DIR" && python3 -c "import app" 2>&1 ); then
    echo "ERROR: app.py import test failed (missing Python dependencies?)"
    echo "       Run: pip install --break-system-packages -r requirements.txt"
    rollback
fi
echo "      Syntax OK, imports OK"

# ─── 6. Start server ──────────────────────────────────────────────────────
echo "[6/6] Starting server..."
cd "$INSTALL_DIR"
nohup python3 app.py > console.log 2>&1 &
NEW_PID=$!
sleep 4

if ! kill -0 "$NEW_PID" 2>/dev/null && ! pgrep -f "python.*app\.py" >/dev/null 2>&1; then
    echo "ERROR: server didn't start. Last 20 log lines:"
    tail -n 20 "$INSTALL_DIR/console.log" 2>/dev/null || true
    rollback
fi

# Quick health check
if command -v curl >/dev/null 2>&1; then
    HTTP_CODE="$(curl -sS -o /dev/null -w '%{http_code}' http://localhost:5000/health 2>/dev/null || echo '000')"
    if [ "$HTTP_CODE" = "200" ]; then
        echo "      Health check: OK (HTTP 200)"
    else
        echo "      Health check: HTTP $HTTP_CODE (server may still be starting; check console.log)"
    fi
fi

echo
echo "============================================================"
echo "✓ Update complete!"
echo "============================================================"
echo
echo "  Install:   $INSTALL_DIR  ($NEW_VERSION)"
echo "  Backup:    $BACKUP_DIR"
echo "  Log:       $INSTALL_DIR/console.log"
echo
echo "  Customer state preserved:"
echo "    ✓ Users, sessions, audit log"
echo "    ✓ Query history, connection registry"
echo "    ✓ License file, instance fingerprint"
echo "    ✓ Encrypted credentials (master.key)"
echo
echo "  Once you've confirmed everything works, remove the backup:"
echo "    rm -rf \"$BACKUP_DIR\""
echo
