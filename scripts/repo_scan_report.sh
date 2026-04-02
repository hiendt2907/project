#!/usr/bin/env bash
# Repo scan: YAML inventory + vulture (optional). Không xóa file — chỉ ghi báo cáo.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${ROOT}/reports/repo_scan_latest.txt"
mkdir -p "$(dirname "$OUT")"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi
{
  echo "repo_scan $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo ""
  echo "=== YAML files (excluding .venv / .git) ==="
  find "$ROOT" \( -name '*.yaml' -o -name '*.yml' \) \
    ! -path '*/.venv/*' ! -path '*/.git/*' 2>/dev/null | sort | head -n 5000
  echo ""
  echo "=== vulture src (min-confidence 80) ==="
  if "$PY" -c "import vulture" 2>/dev/null; then
    "$PY" -m vulture "$ROOT/src" --min-confidence 80 2>/dev/null || true
  else
    echo "(install: pip install vulture)"
  fi
} | tee "$OUT"
echo "Wrote $OUT"
