#!/usr/bin/env bash
# Chaos drill — Lane 2 (SYS_HARD_FAIL) bare metal OS scenarios.
#
# Injects evidence for systemd failure, disk critical, swap exhaustion,
# and OOM kill events. These represent failures on bare metal / VM hosts
# that Omni monitors via remote agents — NOT K8s workloads.
#
# Each scenario posts a synthetic alert via chaos_lane_drill.py and
# verifies the system generates an advisory within the SLO budget.
#
# Safety gates (exit 2 if violated):
#   OMNI_ENV_MODE=lab
#   OMNI_AUTO_EXECUTE_ENABLED=false
#
# Usage:
#   OMNI_ENV_MODE=lab OMNI_AUTO_EXECUTE_ENABLED=false bash scripts/chaos/chaos_lane2_os_baremetal.sh
#   OMNI_ENV_MODE=lab OMNI_AUTO_EXECUTE_ENABLED=false bash scripts/chaos/chaos_lane2_os_baremetal.sh --scenario disk
#   OMNI_ENV_MODE=lab OMNI_AUTO_EXECUTE_ENABLED=false bash scripts/chaos/chaos_lane2_os_baremetal.sh --dry-run

set -euo pipefail

# ── Safety gates ──────────────────────────────────────────────────────────────

[ "${OMNI_ENV_MODE:-}" = "lab" ] || {
    echo "ABORT: OMNI_ENV_MODE must be 'lab' (current: '${OMNI_ENV_MODE:-unset}')"
    exit 2
}

[ "${OMNI_AUTO_EXECUTE_ENABLED:-}" = "false" ] || {
    echo "ABORT: OMNI_AUTO_EXECUTE_ENABLED must be 'false' — auto-execute must be off during chaos"
    exit 2
}

# ── Config ────────────────────────────────────────────────────────────────────

GATEWAY_URL="${OMNI_GATEWAY_URL:-http://gateway.ai-agent.local}"
DRILL_CMD=".venv/bin/python scripts/chaos_lane_drill.py"
SCENARIO="${1:-all}"
DRY_RUN="${DRY_RUN:-}"
DRY_FLAG=""

# Parse args
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
    echo "[OS BAREMETAL CHAOS] lane=${lane}"
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
echo "Omni Chaos Drill — Lane 2 OS Baremetal"
echo "  Gateway:    ${GATEWAY_URL}"
echo "  Dry-run:    ${DRY_FLAG:-false}"
echo "  Scenario:   ${SCENARIO}"
echo "═══════════════════════════════════════════════════════════════════"

case "$SCENARIO" in
    all)
        _run_scenario "hardfail-systemd"
        _run_scenario "hardfail-disk"
        _run_scenario "hardfail-swap"
        _run_scenario "hardfail-oom"
        ;;
    systemd) _run_scenario "hardfail-systemd" ;;
    disk)    _run_scenario "hardfail-disk"    ;;
    swap)    _run_scenario "hardfail-swap"    ;;
    oom)     _run_scenario "hardfail-oom"     ;;
    *)
        echo "Unknown scenario: ${SCENARIO}. Valid: all, systemd, disk, swap, oom"
        exit 1
        ;;
esac

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "OS BAREMETAL CHAOS SUMMARY: PASS=${PASS} FAIL=${FAIL}"
echo "═══════════════════════════════════════════════════════════════════"

[ "$FAIL" -eq 0 ] || exit 1
