#!/usr/bin/env bash
# Run full pytest suite and write JUnit + log + run metadata under evidence/latest/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "run_test_evidence: no .venv/bin/python or python3" >&2
  exit 1
fi

OUT="$ROOT/evidence/latest"
mkdir -p "$OUT"

{
  echo "commit=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "date_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "uname=$(uname -s 2>/dev/null || echo unknown)"
  echo "python=$PY"
} >"$OUT/run-meta.txt"

set +e
"$PY" -m pytest tests -v --junitxml="$OUT/junit.xml" 2>&1 | tee "$OUT/pytest.log"
rc=${PIPESTATUS[0]}
set -e
echo "pytest_exit=$rc" >>"$OUT/run-meta.txt"
exit "$rc"
