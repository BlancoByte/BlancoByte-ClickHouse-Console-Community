#!/usr/bin/env bash
# Pre-bundle validation (item 10). Catches the syntax/structure regressions
# that used to only be caught by hand before zipping a release.
#   - Python: app.py and every *.py parses
#   - JS: every <script> block in static/index.html passes `node --check`
# Exits non-zero on the first failure so CI fails the build.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "→ Python syntax check"
for f in $(find . -name '*.py' -not -path './venv/*' -not -path '*/__pycache__/*'); do
  python3 -c "import ast,sys; ast.parse(open('$f').read())" || { echo "PY FAIL: $f"; exit 1; }
done
echo "  ok"

echo "→ JS syntax check (extracted <script> blocks)"
python3 - <<'PY'
import re, sys
html = open('static/index.html', encoding='utf-8').read()
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
open('/tmp/_bundle_check.js', 'w', encoding='utf-8').write('\n;\n'.join(scripts))
print(f"  extracted {len(scripts)} script block(s)")
PY
if command -v node >/dev/null 2>&1; then
  if node --check /tmp/_bundle_check.js; then echo "  ok"; else echo "JS SYNTAX FAIL"; exit 1; fi
else
  echo "  (node not found — skipping JS check)"; exit 1
fi

# Optional: pyflakes if installed (non-fatal lint signal)
if command -v pyflakes >/dev/null 2>&1; then
  echo "→ pyflakes (lint)"; pyflakes app.py || true
fi

echo "✓ validation passed"
