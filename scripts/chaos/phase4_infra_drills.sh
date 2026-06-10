#!/usr/bin/env bash
# phase4_infra_drills.sh — Master orchestrator: Phase 4 OS/DB/Network Drills (D12).
#
# ╔══════════════════════════════════════════════════════════════════════╗
# ║  TẤT CẢ DRILLS Ở PHASE 4 LÀ SIMULATOR-BASED                       ║
# ║                                                                      ║
# ║  [SIMULATOR] — inject synthetic Prometheus alert payload            ║
# ║    via gateway /webhook/prometheus.                                  ║
# ║                                                                      ║
# ║  TẠI SAO SIMULATOR?                                                  ║
# ║  Lab không có bare metal OS, MySQL server, NFS mount thật.         ║
# ║  Omni nhận evidence qua Kafka/gateway, không trực tiếp check OS.   ║
# ║  Simulator test đúng luồng xử lý business logic.                   ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# Usage:
#   OMNI_ENV_MODE=lab OMNI_AUTO_EXECUTE_ENABLED=false \
#     bash scripts/chaos/phase4_infra_drills.sh
#
#   DRILL_GROUPS=database bash scripts/chaos/phase4_infra_drills.sh
#   DRILL_GROUPS=os,network bash scripts/chaos/phase4_infra_drills.sh
#   DRY_RUN=true bash scripts/chaos/phase4_infra_drills.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
NS="${NS:-multi-agent}"
GATEWAY_URL="${OMNI_GATEWAY_URL:-http://gateway.ai-agent.local}"
DRILL_GROUPS="${DRILL_GROUPS:-all}"
DRY_RUN="${DRY_RUN:-false}"
DRILL_CMD="${PROJECT_DIR}/.venv/bin/python ${PROJECT_DIR}/scripts/chaos_lane_drill.py"

# ── Safety gates ──────────────────────────────────────────────────────────────
[ "${OMNI_ENV_MODE:-}" = "lab" ] || {
    echo "[PHASE4] ABORT: OMNI_ENV_MODE must be 'lab'" >&2; exit 2
}
[ "${OMNI_AUTO_EXECUTE_ENABLED:-true}" = "false" ] || {
    echo "[PHASE4] ABORT: OMNI_AUTO_EXECUTE_ENABLED must be 'false'" >&2; exit 2
}

DRY_FLAG=""
[ "$DRY_RUN" = "true" ] && DRY_FLAG="--dry-run"

# Lane definitions (plain arrays — bash 3.2 compatible)
# Format: "lane:group:description"
LANE_DEFS=(
    "resource-baremetal:os:CPU/Mem resource anomaly on bare metal host"
    "hardfail-systemd:os:systemd unit failed (critical service down)"
    "hardfail-disk:os:disk critical — partitions full or inode exhausted"
    "hardfail-swap:os:swap exhausted — 100% usage OOM risk"
    "hardfail-oom:os:OOM kill event — kernel terminated process"
    "hardfail-mysql:database:MySQL connection refused / replication lag"
    "hardfail-proxysql:database:ProxySQL backend pool degraded"
    "hardfail-postgresql:database:PostgreSQL replica lag / connection exhaustion"
    "hardfail-mongodb:database:MongoDB replica set degraded"
    "hardfail-nfs:network:NFS stale mount / server unreachable"
    "hardfail-dns:network:DNS resolution failure on resolver"
    "hardfail-haproxy:network:HAProxy backend servers down"
)

_lane_name()  { echo "$1" | cut -d: -f1; }
_lane_group() { echo "$1" | cut -d: -f2; }
_lane_desc()  { echo "$1" | cut -d: -f3-; }

# ── Pre-flight ────────────────────────────────────────────────────────────────
echo "[PHASE4] Pre-flight checks..."

if [ "$DRY_RUN" != "true" ]; then
    GW_HEALTH=$(curl -sf "${GATEWAY_URL}/healthz" 2>/dev/null \
        | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null \
        || echo "unreachable")
    if [ "$GW_HEALTH" = "unreachable" ]; then
        echo "[PHASE4] ABORT: Gateway not reachable at $GATEWAY_URL" >&2
        echo "[PHASE4]   Run: kubectl port-forward -n $NS svc/omni-gateway 8080:80" >&2
        exit 1
    fi
    echo "[PHASE4] ✓ gateway healthy (status=$GW_HEALTH)"
fi

VENV_PY="${PROJECT_DIR}/.venv/bin/python"
[ -f "$VENV_PY" ] || { echo "[PHASE4] ABORT: venv not found at $VENV_PY" >&2; exit 1; }
echo "[PHASE4] ✓ venv python found"
echo ""

# ── Drill tracking ────────────────────────────────────────────────────────────
PHASE_START=$(date +%s)
declare -a RESULTS=()
TOTAL_PASS=0
TOTAL_FAIL=0
TOTAL_SKIP=0

_should_run() {
    local GROUP="$1"
    [ "$DRILL_GROUPS" = "all" ] && return 0
    # Check if group appears in comma-separated DRILL_GROUPS
    echo ",$DRILL_GROUPS," | grep -q ",${GROUP},"
}

_run_lane() {
    local DEF="$1"
    local LANE GROUP DESC
    LANE=$(_lane_name "$DEF")
    GROUP=$(_lane_group "$DEF")
    DESC=$(_lane_desc "$DEF")

    if ! _should_run "$GROUP"; then
        RESULTS+=("SKIP:${LANE}:${GROUP}:${DESC}")
        TOTAL_SKIP=$((TOTAL_SKIP+1))
        return
    fi

    echo ""
    echo "  ── [SIMULATOR] ${LANE} (group=${GROUP}) ──────────────────────────────"
    echo "     ${DESC}"
    echo "     ⚠ Synthetic payload — tests business logic, NOT real ${GROUP} infra"

    local T_START RC
    T_START=$(date +%s)
    RC=0

    OMNI_GATEWAY_URL="$GATEWAY_URL" \
    $DRILL_CMD --lane "$LANE" $DRY_FLAG 2>&1 || RC=$?

    local T_END ELAPSED
    T_END=$(date +%s)
    ELAPSED=$(( T_END - T_START ))

    if [ "$RC" -eq 0 ]; then
        echo "  [PASS] ${LANE} (${ELAPSED}s)"
        RESULTS+=("PASS:${LANE}:${GROUP}:${ELAPSED}s:${DESC}")
        TOTAL_PASS=$((TOTAL_PASS+1))
    else
        echo "  [FAIL] ${LANE} (${ELAPSED}s, exit=${RC})"
        RESULTS+=("FAIL:${LANE}:${GROUP}:${ELAPSED}s:${DESC}")
        TOTAL_FAIL=$((TOTAL_FAIL+1))
    fi
}

# ── Run all lanes ─────────────────────────────────────────────────────────────
echo "[PHASE4] Starting infra simulator drills..."
echo "         Groups: $DRILL_GROUPS | Dry-run: $DRY_RUN"
echo ""

for DEF in "${LANE_DEFS[@]}"; do
    _run_lane "$DEF"
done

# ── Final report ──────────────────────────────────────────────────────────────
PHASE_END=$(date +%s)
PHASE_ELAPSED=$(( PHASE_END - PHASE_START ))

echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║      PHASE 4 — INFRA DRILLS — BÁOCÁO NGHIỆM THU (SIMULATOR)   ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "  Ngày giờ  : $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Namespace : $NS"
echo "  Gateway   : $GATEWAY_URL"
echo "  Groups    : $DRILL_GROUPS"
echo "  Dry-run   : $DRY_RUN"
echo "  Thời gian : ${PHASE_ELAPSED}s tổng cộng"
echo ""
echo "  ⚠ TẤT CẢ TESTS: [SIMULATOR] — synthetic payloads, không phải real infra"
echo ""

# Print grouped results
for GROUP_NAME in os database network; do
    echo "  ── Group: ${GROUP_NAME} ──────────────────────────────────────────────"
    for R in "${RESULTS[@]}"; do
        if echo "$R" | grep -q ":${GROUP_NAME}:"; then
            VERDICT=$(echo "$R" | cut -d: -f1)
            LANE=$(echo "$R" | cut -d: -f2)
            TIME_OR_REASON=$(echo "$R" | cut -d: -f4)
            if [ "$VERDICT" = "SKIP" ]; then
                printf "    %-6s  %-30s  (skipped)\n" "$VERDICT" "$LANE"
            else
                printf "    %-6s  %-30s  %s\n" "$VERDICT" "$LANE" "$TIME_OR_REASON"
            fi
        fi
    done
    echo ""
done

echo "  Tổng kết: PASS=${TOTAL_PASS}  FAIL=${TOTAL_FAIL}  SKIP=${TOTAL_SKIP}"
echo ""

if [ "$TOTAL_FAIL" -eq 0 ]; then
    echo "  ✓ PHASE 4 PASS"
    PHASE_VERDICT="PASS"
else
    echo "  ✗ PHASE 4 FAIL — ${TOTAL_FAIL} lane(s) failed"
    PHASE_VERDICT="FAIL"
fi

echo ""
echo "  ⚠ SIMULATOR NOTICE:"
echo "    Tất cả Phase 4 drills inject synthetic alert payload."
echo "    Để test real infra: cần remote agent trên real OS/DB host."
echo "    Phase 4 simulator xác nhận advisory pipeline, không xác nhận"
echo "    khả năng detect failure thật từ bare metal / database."
echo ""
echo "══════════════════════════════════════════════════════════════════"

[ "$PHASE_VERDICT" = "PASS" ] || exit 1
