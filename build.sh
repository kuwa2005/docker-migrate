#!/usr/bin/env bash
# Build a single-file docker-migrate zipapp for distribution.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGING="$(mktemp -d)"
OUT_DIR="${ROOT}/dist"
OUT_FILE="${OUT_DIR}/docker-migrate"

cleanup() {
  rm -rf "$STAGING"
}
trap cleanup EXIT

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 が見つかりません。" >&2
  exit 1
fi

cp -a "${ROOT}/docker_migrate" "${STAGING}/"
find "$STAGING" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGING" -name '*.pyc' -delete 2>/dev/null || true

cat > "${STAGING}/__main__.py" <<'EOF'
from docker_migrate.__main__ import main

raise SystemExit(main())
EOF

mkdir -p "$OUT_DIR"
python3 -m zipapp "$STAGING" \
  -p "/usr/bin/env python3" \
  -o "$OUT_FILE"
chmod +x "$OUT_FILE"

echo "Built single-file zipapp: ${OUT_FILE}"
echo "Copy only this file for standalone use:"
echo "  ${OUT_FILE}"
