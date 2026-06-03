#!/usr/bin/env bash
# phase3_k8s_drills.sh — Master orchestrator: Phase 3 Real K8s Drills (D10-D11).
#
# Chạy tất cả K8s drill theo thứ tự, tổng hợp báo cáo nghiệm thu.
#
# ╔══════════════════════════════════════════════════════════════════════╗
# ║  PHÂN LOẠI TEST THEO LOGIC:                                         ║
# ║                                                                      ║
# ║  [INVERTED LOGIC] — test bằng cách inject synthetic condition       ║
# ║    vì real condition không đạt được trong lab (no real load).       ║
# ║    Kiểm tra BUSINESS LOGIC, không kiểm tra metric collection.       ║
# ║                                                                      ║
# ║  [REAL LOGIC]     — test bằng cách tạo real condition trong K8s.   ║
# ║    Kiểm tra END-TO-END behavior thật.                               ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# Drills:
#   D10.1  Lane 1 CPU Resource Anomaly         [INVERTED LOGIC]
#   D10.2  Lane 1 Snapshot Kill (fail-closed)  [REAL LOGIC]
#   D10.3  Lane 1 Maintenance Window           [REAL LOGIC]
#   D11    Lane 2 CrashLoopBackOff             [INVERTED LOGIC — alert injected]
#
# Usage:
#   OMNI_ENV_MODE=lab OMNI_AUTO_EXECUTE_ENABLED=false \
#     bash scripts/chaos/phase3_k8s_drills.sh
#
#   # Skip slow drills (snapshot kill takes ~310s wait):
#   SKIP_SNAPSHOT_KILL=true bash scripts/chaos/phase3_k8s_drills.sh
#
# Prerequisites:
#   - kubectl context pointing to lab cluster
#   - omni-analyst pod Running in multi-agent
#   - gateway port-forward on OMNI_GATEWAY_URL (default localhost:8080)
#   - redis port-forward on OMNI_REDIS_HOST:OMNI_REDIS_PORT (default localhost:16379)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS="${NS:-multi-agent}"
GATEWAY_URL="${OMNI_GATEWAY_URL:-http://localhost:8080}"
SKIP_SNAPSHOT_KILL="${SKIP_SNAPSHOT_KILL:-false}"

# ── Safety gates ──────────────────────────────────────────────────────────────
[ "${OMNI_ENV_MODE:-}" = "lab" ] || {
    echo "[PHASE3] ABORT: OMNI_ENV_MODE must be 'lab'" >&2; exit 2
}
[ "${OMNI_AUTO_EXECUTE_ENABLED:-true}" = "false" ] || {
    echo "[PHASE3] ABORT: OMNI_AUTO_EXECUTE_ENABLED must be 'false'" >&2; exit 2
}

# ── Pre-flight check ──────────────────────────────────────────────────────────
echo "[PHASE3] Pre-flight checks..."

ANALYST_RUNNING=$(kubectl get pods -n "$NS" -l app=omni-fullstack --no-headers 2>/dev/null \
    | grep -c "Running" || echo "0")
if [ "$ANALYST_RUNNING" -eq "0" ]; then
    echo "[PHASE3] ABORT: omni-analyst not Running in $NS" >&2
    kubectl get pods -n "$NS" -l app=omni-fullstack 2>/dev/null >&2
    exit 1
fi

GW_HEALTH=$(curl -sf "${GATEWAY_URL}/healthz" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "unreachable")
if [ "$GW_HEALTH" = "unreachable" ] || [ -z "$GW_HEALTH" ]; then
    echo "[PHASE3] ABORT: Gateway not reachable at $GATEWAY_URL" >&2
    echo "[PHASE3]   Run: kubectl port-forward -n $NS svc/omni-gateway 8080:8080" >&2
    exit 1
fi

echo "[PHASE3] ✓ omni-analyst Running ($ANALYST_RUNNING pod)"
echo "[PHASE3] ✓ gateway healthy (status=$GW_HEALTH)"
echo ""

# ── Drill result tracking ─────────────────────────────────────────────────────
PHASE_START=$(date +%s)
declare -a DRILL_RESULTS=()
TOTAL_PASS=0
TOTAL_FAIL=0
TOTAL_SKIP=0

_run_drill() {
    local DRILL_ID="$1"
    local LABEL="$2"
    local MODE="$3"      # [INVERTED LOGIC] or [REAL LOGIC]
    local SCRIPT="$4"
    local SKIP_FLAG="${5:-false}"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Drill ${DRILL_ID}: ${LABEL}"
    echo "  Mode : ${MODE}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    if [ "$SKIP_FLAG" = "true" ]; then
        echo "  [SKIPPED] SKIP_${DRILL_ID//./_} flag set"
        DRILL_RESULTS+=("  SKIP   ${DRILL_ID}  ${LABEL}  ${MODE}")
        TOTAL_SKIP=$((TOTAL_SKIP+1))
        return
    fi

    local T_START; T_START=$(date +%s)
    local RC=0

    OMNI_ENV_MODE=lab OMNI_AUTO_EXECUTE_ENABLED=false \
        bash "$SCRIPT" 2>&1 || RC=$?

    local T_END; T_END=$(date +%s)
    local ELAPSED=$(( T_END - T_START ))

    if [ "$RC" -eq 0 ]; then
        DRILL_RESULTS+=("  PASS   ${DRILL_ID}  ${ELAPSED}s  ${LABEL}  ${MODE}")
        TOTAL_PASS=$((TOTAL_PASS+1))
    else
        DRILL_RESULTS+=("  FAIL   ${DRILL_ID}  ${ELAPSED}s  ${LABEL}  ${MODE}")
        TOTAL_FAIL=$((TOTAL_FAIL+1))
    fi
}

# ── Run drills ────────────────────────────────────────────────────────────────
_run_drill "D10.1" "Lane 1 CPU Resource Anomaly" \
    "[INVERTED LOGIC]" \
    "${SCRIPT_DIR}/chaos_lane1_resource.sh"

_run_drill "D10.2" "Lane 1 Snapshot Kill (fail-closed)" \
    "[REAL LOGIC]" \
    "${SCRIPT_DIR}/chaos_lane1_snapshot_kill.sh" \
    "$SKIP_SNAPSHOT_KILL"

_run_drill "D10.3" "Lane 1 Maintenance Window" \
    "[REAL LOGIC]" \
    "${SCRIPT_DIR}/chaos_lane1_maint.sh"

_run_drill "D11" "Lane 2 CrashLoopBackOff" \
    "[INVERTED LOGIC — alert injected]" \
    "${SCRIPT_DIR}/chaos_lane2_crashloop.sh"

# ── Final report ──────────────────────────────────────────────────────────────
PHASE_END=$(date +%s)
PHASE_ELAPSED=$(( PHASE_END - PHASE_START ))

echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║         PHASE 3 — K8s DRILLS — BÁOCÁO NGHIỆM THU               ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "  Ngày giờ  : $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Namespace : $NS"
echo "  Cluster   : $(kubectl config current-context 2>/dev/null || echo 'unknown')"
echo "  Thời gian : ${PHASE_ELAPSED}s tổng cộng"
echo ""
echo "  LEGEND:"
echo "  [INVERTED LOGIC] = synthetic condition injected, tests business logic"
echo "  [REAL LOGIC]     = real K8s condition created, tests end-to-end"
echo ""
echo "  ┌──────────────────────────────────────────────────────────────┐"
printf "  │ %-6s  %-6s  %-6s  %-36s  %-22s│\n" "RESULT" "DRILL" "TIME" "NAME" "MODE"
echo "  │──────────────────────────────────────────────────────────────│"
for line in "${DRILL_RESULTS[@]}"; do
    echo "  │${line}"
done
echo "  └──────────────────────────────────────────────────────────────┘"
echo ""
echo "  Tổng kết: PASS=${TOTAL_PASS}  FAIL=${TOTAL_FAIL}  SKIP=${TOTAL_SKIP}"
echo ""

if [ "$TOTAL_FAIL" -eq 0 ]; then
    echo "  ✓ PHASE 3 PASS — tất cả drills passed"
    PHASE_VERDICT="PASS"
else
    echo "  ✗ PHASE 3 FAIL — ${TOTAL_FAIL} drill(s) failed"
    PHASE_VERDICT="FAIL"
fi

echo ""
echo "  ⚠ LƯU Ý VỀ INVERTED LOGIC TESTS:"
echo "    D10.1 và D11 inject condition tổng hợp thay vì real load."
echo "    Các test này xác nhận BUSINESS LOGIC (advisory pipeline),"
echo "    KHÔNG xác nhận metric collection từ Prometheus thật."
echo "    Để test metric collection thật: cần production workload thật."
echo ""
echo "══════════════════════════════════════════════════════════════════"

[ "$PHASE_VERDICT" = "PASS" ] || exit 1
