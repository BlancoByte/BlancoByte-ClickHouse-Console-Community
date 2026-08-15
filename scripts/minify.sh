#!/usr/bin/env bash
# Optional build step (item 7): produce a minified single-file app.
#   static/index.html  ->  static/index.min.html  (~30% smaller pre-gzip)
# Only writes the output if the minified JS still parses, so a bad minify
# can never ship. Enable serving it with the env var SERVE_MINIFIED=1.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="static/index.html"
TMP="/tmp/index.min.candidate.html"
OUT="static/index.min.html"

echo "→ Minifying $SRC"
npx --yes html-minifier-terser@7 \
  --collapse-whitespace --remove-comments \
  --minify-css true --minify-js true \
  -o "$TMP" "$SRC"

echo "→ Verifying minified JS parses"
python3 - "$TMP" <<'PY'
import re, sys
html = open(sys.argv[1], encoding='utf-8').read()
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
open('/tmp/_min_check.js', 'w', encoding='utf-8').write('\n;\n'.join(scripts))
PY
if node --check /tmp/_min_check.js; then
  mv "$TMP" "$OUT"
  python3 - "$SRC" "$OUT" <<'PY'
import os, sys
a=os.path.getsize(sys.argv[1]); b=os.path.getsize(sys.argv[2])
print(f"✓ wrote {sys.argv[2]}  ({a} -> {b} bytes, {100-b*100//a}% smaller)")
PY
else
  echo "✗ minified JS failed to parse — keeping previous $OUT (if any), not shipping broken output"
  rm -f "$TMP"; exit 1
fi
