#!/usr/bin/env bash
# Chaos drill — Lane 2 (SYS_HARD_FAIL) network and storage scenarios.
#
# Injects evidence for NFS stale mounts, DNS resolution failure,
# and HAProxy backend outage. These represent failures in the
# network and storage layer that Omni monitors via remote agents.
#
# Safety gates (exit 2 if violated):
#   OMNI_ENV_MODE=lab
#   OMNI_AUTO_EXECUTE_ENABLED=false
#
# Usage:
#   OMNI_ENV_MODE=lab OMNI_AUTO_EXECUTE_ENABLED=false bash scripts/chaos/chaos_lane2_network.sh
#   OMNI_ENV_MODE=lab OMNI_AUTO_EXECUTE_ENABLED=false bash scripts/chaos/chaos_lane2_network.sh --scenario dns
#   OMNI_ENV_MODE=lab OMNI_AUTO_EXECUTE_ENABLED=false bash scripts/chaos/chaos_lane2_network.sh --dry-run

set -euo pipefail

# ── Safety gates ──────────────────────────────────────────────────────────────

[ "${OMNI_ENV_MODE:-}" = "lab" ] || {
    echo "ABORT: OMNI_ENV_MODE must be 'lab' (current: '${OMNI_ENV_MODE:-unset}')"
    exit 2
}

[ "${OMNI_AUTO_EXECUTE_ENABLED:-}" = "false" ] || {
    echo "ABORT: OMNI_AUTO_EXECUTE_ENABLED must be 'false'"
    exit 2
}

# ── Config ────────────────────────────────────────────────────────────────────

GATEWAY_URL="${OMNI_GATEWAY_URL:-http://gateway.ai-agent.local}"
DRILL_CMD=".venv/bin/python scripts/chaos_lane_drill.py"
SCENARIO="all"
DRY_FLAG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scenario) SCENARIO="$2"; shift 2 ;;
        --dry-run)  DRY_FLAG="--dry-run"; shift ;;
        *)          shift ;;
    esac
done

PASS=0
FAIL=0

_run_scenario() {
    local lane="$1"
    echo ""
    echo "══════════════════════════════════════════════════════════════"
    echo "[NETWORK/STORAGE CHAOS] lane=${lane}"
    echo "══════════════════════════════════════════════════════════════"
    # shellcheck disable=SC2086
    if $DRILL_CMD --lane "${lane}" ${DRY_FLAG}; then
        echo "[PASS] ${lane}"
        PASS=$((PASS + 1))
    else
        echo "[FAIL] ${lane}"
        FAIL=$((FAIL + 1))
    fi
}

# ── Scenarios ─────────────────────────────────────────────────────────────────

echo "═══════════════════════════════════════════════════════════════════"
echo "Omni Chaos Drill — Lane 2 Network / Storage"
echo "  Gateway:    ${GATEWAY_URL}"
echo "  Dry-run:    ${DRY_FLAG:-false}"
echo "  Scenario:   ${SCENARIO}"
echo "═══════════════════════════════════════════════════════════════════"

case "$SCENARIO" in
    all)
        _run_scenario "hardfail-nfs"
        _run_scenario "hardfail-dns"
        _run_scenario "hardfail-haproxy"
        ;;
    nfs)     _run_scenario "hardfail-nfs"     ;;
    dns)     _run_scenario "hardfail-dns"     ;;
    haproxy) _run_scenario "hardfail-haproxy" ;;
    *)
        echo "Unknown scenario: ${SCENARIO}. Valid: all, nfs, dns, haproxy"
        exit 1
        ;;
esac

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "NETWORK/STORAGE CHAOS SUMMARY: PASS=${PASS} FAIL=${FAIL}"
echo "═══════════════════════════════════════════════════════════════════"

[ "$FAIL" -eq 0 ] || exit 1
