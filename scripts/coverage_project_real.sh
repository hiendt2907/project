#!/usr/bin/env bash
# Run coverage against the checked-out tree on this machine.
# Python: pytest-cov with .coveragerc.gate (same as make coverage-gate).
# Optional: integration tests when OMNI_REDIS_URL is set (see tests/real_services/).
# Smart-SIEM: go test -cover per module under smart-siem/ (requires Go toolchain).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Python (Omni) coverage-gate scope"
rm -f "$ROOT/.coverage"
export PYTHONPATH="${PYTHONPATH:-}:$ROOT/src:$ROOT/scripts"
# Integration + unit: include real_services when Redis URL present
if [[ -n "${OMNI_REDIS_URL:-}" ]]; then
  "$ROOT/.venv/bin/python" -m pytest "$ROOT/tests" \
    --ignore=tests/integration \
    -q \
    --cov=src \
    --cov-config="$ROOT/.coveragerc.gate" \
    --cov-report=term
else
  echo "(OMNI_REDIS_URL unset — skipping live Redis tests; export it to include tests/real_services)"
  "$ROOT/.venv/bin/python" -m pytest "$ROOT/tests" \
    --ignore=tests/integration \
    -q \
    --cov=src \
    --cov-config="$ROOT/.coveragerc.gate" \
    --cov-report=term
fi

if [[ -d "$ROOT/smart-siem" ]] && command -v go >/dev/null 2>&1; then
  echo ""
  echo "==> Go modules (smart-siem) — coverage summary per module"
  export GOTOOLCHAIN=local
  while IFS= read -r mod; do
    [[ -z "$mod" || "$mod" =~ ^# ]] && continue
    modpath="${mod#./}"
    abs="$ROOT/smart-siem/$modpath"
    [[ -d "$abs" && -f "$abs/go.mod" ]] || continue
    echo "--- $modpath ---"
    (cd "$abs" && go test ./... -coverprofile=coverage.out -covermode=atomic -count=1 2>/dev/null && go tool cover -func=coverage.out | tail -1) || echo "(skip or failed)"
    rm -f "$abs/coverage.out"
  done <<'MODULES'
./omni/siem/bff
./omni/siem/agent
./omni/siem/brain-go
./omni/siem/math-gateway
./omni/siem/license-validator
./customer/ui
MODULES
else
  echo "Go or smart-siem/ missing — skip Go coverage block."
fi
