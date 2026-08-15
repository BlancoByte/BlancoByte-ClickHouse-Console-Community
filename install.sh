#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")"; pwd)"
cd "$SCRIPT_DIR"
echo "=========================================="
echo "  ClickHouse-Console by BlancoByte"
echo "  v3.1 installer"
echo "=========================================="
echo "Installing in: $SCRIPT_DIR"
echo

# --- Pre-flight checks ----------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ python3 not found. Install it first:"
  echo "    apt install -y python3 python3-venv      (Debian/Ubuntu)"
  echo "    yum install -y python3 python3-venv      (RHEL/Rocky)"
  exit 1
fi

# Verify venv module works (PEP 668 distros split it into python3-venv pkg)
if ! python3 -c "import venv" 2>/dev/null; then
  echo "✗ python3 venv module missing."
  echo "  On Debian/Ubuntu run:"
  echo "    apt install -y python3-venv python3-full"
  echo "  On RHEL/Rocky:"
  echo "    yum install -y python3-venv"
  exit 1
fi

# --- Install ---------------------------------------------------------------
echo "→ Creating virtualenv at .venv/"
python3 -m venv .venv
# shellcheck source=/dev/null
source .venv/bin/activate
echo "→ Upgrading pip"
pip install --quiet --upgrade pip
echo "→ Installing dependencies (flask, clickhouse-connect, gunicorn, cryptography...)"
pip install --quiet -r requirements.txt
deactivate

# --- Helper scripts --------------------------------------------------------
cat > run.sh << 'SH'
#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
export PYTHONDONTWRITEBYTECODE=1
exec python3 app.py "$@"
SH
chmod +x run.sh

cat > run-prod.sh << 'SH'
#!/bin/bash
# Production start: gunicorn, 4 workers x 8 threads on :5000
cd "$(dirname "$0")"
source .venv/bin/activate
exec gunicorn -w 4 -k gthread --threads 8 -b 0.0.0.0:5000 \
              --access-logfile - --capture-output --timeout 120 app:app
SH
chmod +x run-prod.sh

cat > logtail.sh << 'SH'
#!/bin/bash
tail -f "$(dirname "$0")/console.log"
SH
chmod +x logtail.sh

cat > admin.sh << 'SH'
#!/bin/bash
# Wrapper for app.py CLI commands (list-users, create-user, reset-password, ...)
cd "$(dirname "$0")"
source .venv/bin/activate
exec python3 app.py "$@"
SH
chmod +x admin.sh

cat > monitor.sh << 'SH'
#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
exec python3 ch_monitor.py "$@"
SH
chmod +x monitor.sh

cat > schema.sh << 'SH'
#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
exec python3 ch_schema.py "$@"
SH
chmod +x schema.sh

echo
echo "=========================================="
echo "✓ Installation complete"
echo "=========================================="
echo
echo "  ./run.sh                 → Dev server (http://localhost:5000)"
echo "  ./run-prod.sh            → Production server (gunicorn)"
echo "  ./admin.sh list-users    → CLI user management"
echo "  ./admin.sh create-user alice --role developer"
echo "  ./admin.sh reset-password admin"
echo "  ./logtail.sh             → Tail application log"
echo "  ./monitor.sh / ./schema.sh   → CLI tools"
echo
echo "Default login (CHANGE IMMEDIATELY):"
echo "  username: admin   password: admin"
echo
