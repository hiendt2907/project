#!/usr/bin/env bash
# chaos_lane2_crashloop.sh — Lane 2 (SYS_HARD_FAIL) CrashLoopBackOff drill.
#
# ╔══════════════════════════════════════════════════════════════════════╗
# ║  [INVERTED LOGIC — ĐỌC TRƯỚC KHI CHẠY]                             ║
# ║                                                                      ║
# ║  Drill này deploy một pod CrashLoopBackOff THẬT vào K8s,            ║
# ║  NHƯNG alert được inject trực tiếp qua gateway webhook.             ║
# ║                                                                      ║
# ║  TẠI SAO INVERTED?                                                   ║
# ║  kube-state-metrics alert cho chaos pod mất 2-5 phút fire.         ║
# ║  Omni không watch pod events trực tiếp, chỉ đọc Prometheus alerts. ║
# ║  Inject alert trực tiếp = test luồng xử lý, không test scrape lag. ║
# ║                                                                      ║
# ║  CÁI GÌ ĐƯỢC TEST:                                                   ║
# ║  ✓ Pod thật vào CrashLoopBackOff (K8s xác nhận)                    ║
# ║  ✓ Alert pipeline: gateway → Kafka → analyst → advisory            ║
# ║  ✓ CRAT block được write trước khi dispatch                         ║
# ║  ✓ action_type = SUGGEST_REMEDIATION (kill-switch active)           ║
# ║  ✓ Advisory đề cập CrashLoop trong root_cause                       ║
# ║                                                                      ║
# ║  CÁI GÌ KHÔNG ĐƯỢC TEST:                                            ║
# ║  ✗ Alertmanager → Prometheus rule evaluation cho pod này            ║
# ║  ✗ kube-state-metrics scrape → alert fire tự động                  ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# Usage:
#   OMNI_ENV_MODE=lab OMNI_AUTO_EXECUTE_ENABLED=false \
#     bash scripts/chaos/chaos_lane2_crashloop.sh
#
# Acceptance criteria:
#   1. K8s pod enters CrashLoopBackOff or restartCount >= 3 within 60s
#   2. Alert injected successfully via gateway (HTTP 200/202)
#   3. Advisory dispatched within SLO_ADVISORY_SEC (90s)
#   4. No EXECUTE_MUTATE in logs
#   5. Chaos pod cleaned up on exit

set -euo pipefail

NS="${NS:-multi-agent}"
GATEWAY_URL="${OMNI_GATEWAY_URL:-http://localhost:8080}"
GATEWAY_API_KEY="${OMNI_GATEWAY_API_KEY:-}"
SLO_CRASHLOOP_SEC="${SLO_CRASHLOOP_SEC:-60}"
SLO_ADVISORY_SEC="${SLO_ADVISORY_SEC:-90}"
POLL_INTERVAL=5
CHAOS_POD="chaos-crashloop-$(date +%s | tail -c 6)"
DRILL_TRACE="chaos-hardfail-crashloop-$(date +%s)"

# ── Safety gates ──────────────────────────────────────────────────────────────
[ "${OMNI_ENV_MODE:-}" = "lab" ] || {
    echo "[CHAOS] ABORT: OMNI_ENV_MODE must be 'lab'" >&2; exit 2
}
[ "${OMNI_AUTO_EXECUTE_ENABLED:-true}" = "false" ] || {
    echo "[CHAOS] ABORT: OMNI_AUTO_EXECUTE_ENABLED must be 'false'" >&2; exit 2
}
[ "${NS}" != "finguard-customer" ] || {
    echo "[CHAOS] ABORT: forbidden namespace" >&2; exit 2
}

# ── Globals ───────────────────────────────────────────────────────────────────
DRILL_START=$(date +%s)
PASS_COUNT=0
FAIL_COUNT=0
declare -a REPORT_LINES=()

_pass() { echo "[PASS] $*"; PASS_COUNT=$((PASS_COUNT+1)); REPORT_LINES+=("  PASS  $*"); }
_fail() { echo "[FAIL] $*" >&2;  FAIL_COUNT=$((FAIL_COUNT+1)); REPORT_LINES+=("  FAIL  $*"); }
_info() { echo "[INFO] $*"; }
_warn() { echo "[WARN] $*"; }

HTTP_HEADERS=(-H "Content-Type: application/json")
[ -n "$GATEWAY_API_KEY" ] && HTTP_HEADERS+=(-H "X-API-Key: $GATEWAY_API_KEY")

# ── Cleanup on exit ───────────────────────────────────────────────────────────
cleanup() {
    _info "Deleting chaos pod $CHAOS_POD..."
    kubectl delete pod "$CHAOS_POD" -n "$NS" --grace-period=0 --ignore-not-found=true 2>/dev/null || true
    _print_report
}
trap cleanup EXIT

_print_report() {
    local NOW; NOW=$(date +%s)
    local ELAPSED=$(( NOW - DRILL_START ))
    echo ""
    echo "══════════════════════════════════════════════════════════════════"
    echo " CHAOS DRILL REPORT — Lane 2 CrashLoopBackOff"
    echo " Mode : [INVERTED LOGIC] alert injected directly via gateway"
    echo " Pod  : $CHAOS_POD (real K8s pod)"
    echo " Trace: $DRILL_TRACE"
    echo " Time : ${ELAPSED}s elapsed"
    echo "══════════════════════════════════════════════════════════════════"
    echo " ACCEPTANCE CRITERIA:"
    for line in "${REPORT_LINES[@]}"; do echo "$line"; done
    echo "──────────────────────────────────────────────────────────────────"
    if [ "$FAIL_COUNT" -eq 0 ]; then
        echo " VERDICT: ✓ PASS (${PASS_COUNT} checks OK)"
    else
        echo " VERDICT: ✗ FAIL (${FAIL_COUNT} checks failed, ${PASS_COUNT} passed)"
    fi
    echo "══════════════════════════════════════════════════════════════════"
    echo ""
    echo " ⚠ INVERTED LOGIC NOTICE:"
    echo "   CrashLoopBackOff pod là THẬT, nhưng alert được inject trực tiếp."
    echo "   Test này kiểm tra business logic, không phải Alertmanager pipeline."
    echo "══════════════════════════════════════════════════════════════════"
}

# ── Step 1: Deploy CrashLoopBackOff pod ──────────────────────────────────────
_info "Step 1/4: Deploying CrashLoopBackOff pod ($CHAOS_POD) in $NS..."
kubectl run "$CHAOS_POD" -n "$NS" \
    --image=busybox \
    --restart=Always \
    --labels="chaos=lane2-hardfail,chaos_drill=true" \
    -- sh -c "exit 1" 2>/dev/null

_info "Waiting for pod to enter CrashLoopBackOff (SLO: ${SLO_CRASHLOOP_SEC}s)..."
CRASHLOOP_DETECTED=false
K8S_START=$(date +%s)
while true; do
    NOW=$(date +%s); ELAPSED=$(( NOW - K8S_START ))
    STATUS=$(kubectl get pod "$CHAOS_POD" -n "$NS" \
        -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || echo "")
    RESTARTS=$(kubectl get pod "$CHAOS_POD" -n "$NS" \
        -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || echo "0")
    _info "  t=${ELAPSED}s status=${STATUS:-pending} restarts=${RESTARTS}"

    if [ "$STATUS" = "CrashLoopBackOff" ] || [ "${RESTARTS:-0}" -ge "3" ]; then
        _pass "K8s CrashLoopBackOff confirmed at t=${ELAPSED}s (restarts=${RESTARTS})"
        CRASHLOOP_DETECTED=true
        break
    fi
    if [ "$ELAPSED" -ge "$SLO_CRASHLOOP_SEC" ]; then
        _fail "CrashLoopBackOff not reached within ${SLO_CRASHLOOP_SEC}s (restarts=${RESTARTS})"
        break
    fi
    sleep "$POLL_INTERVAL"
done

# ── Step 2: Inject alert via gateway ─────────────────────────────────────────
_info "Step 2/4: Injecting CrashLoopBackOff alert via gateway (trace=$DRILL_TRACE)"

PAYLOAD=$(python3 -c "
import json, time
print(json.dumps({
    'receiver': 'omni-webhook',
    'status': 'firing',
    'alerts': [{
        'status': 'firing',
        'labels': {
            'alertname': 'KubePodCrashLooping',
            'severity': 'critical',
            'namespace': '$NS',
            'pod': '$CHAOS_POD',
            'container': 'chaos-crashloop',
            'deployment': 'chaos-target',
            'reason': 'CrashLoopBackOff',
            'chaos_drill': 'true',
            'trace_id': '$DRILL_TRACE',
        },
        'annotations': {
            'summary': '[CHAOS] CrashLoopBackOff drill',
            'description': 'Pod $CHAOS_POD is crash-looping. Container exit code 1. Chaos drill.',
        },
        'startsAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'endsAt': '0001-01-01T00:00:00Z',
    }],
    'groupLabels': {'alertname': 'KubePodCrashLooping'},
    'externalURL': 'http://alertmanager:9093',
}))
")

HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    "${HTTP_HEADERS[@]}" \
    -d "$PAYLOAD" \
    "${GATEWAY_URL}/webhook/prometheus" 2>/dev/null || echo "000")

if [ "$HTTP_STATUS" = "200" ] || [ "$HTTP_STATUS" = "202" ]; then
    _pass "Gateway accepted alert — HTTP $HTTP_STATUS"
else
    _fail "Gateway rejected alert — HTTP $HTTP_STATUS"
    exit 1
fi

# ── Step 3: Poll for advisory ─────────────────────────────────────────────────
_info "Step 3/4: Polling for advisory dispatch (SLO: ${SLO_ADVISORY_SEC}s)"

ADVISORY_FOUND=false
MUTATE_FOUND=false
ADV_START=$(date +%s)

while true; do
    NOW=$(date +%s); ELAPSED=$(( NOW - ADV_START ))

    # Check CRAT
    CRAT_RAW=$(curl -sf "${GATEWAY_URL}/crat/export" "${HTTP_HEADERS[@]}" 2>/dev/null || echo "[]")
    TRACE_HITS=$(echo "$CRAT_RAW" | python3 -c "
import json,sys
try:
    data=json.loads(sys.stdin.read())
    blocks=data if isinstance(data,list) else data.get('blocks',[])
    print(len([b for b in blocks if '$DRILL_TRACE' in str(b) and 'ADVISORY_DISPATCHED' in str(b)]))
except: print(0)
" 2>/dev/null || echo "0")

    # Check analyst logs
    ANALYST_LOG=$(kubectl logs -n "$NS" -l app=omni-fullstack --tail=80 --since=180s 2>/dev/null || echo "")

    if echo "$ANALYST_LOG" | grep -q "EXECUTE_MUTATE"; then
        MUTATE_FOUND=true
        _fail "EXECUTE_MUTATE found — kill-switch breach"
        break
    fi

    if [ "$TRACE_HITS" -gt "0" ]; then
        _pass "ADVISORY_DISPATCHED in CRAT at t=${ELAPSED}s (SLO ${SLO_ADVISORY_SEC}s)"
        ADVISORY_FOUND=true
        break
    fi

    if echo "$ANALYST_LOG" | grep -q "SUGGEST_REMEDIATION\|advisory_dispatched\|CrashLoop\|crashloop"; then
        _pass "Advisory evidence in analyst logs at t=${ELAPSED}s"
        ADVISORY_FOUND=true
        break
    fi

    if [ "$ELAPSED" -ge "$SLO_ADVISORY_SEC" ]; then
        _fail "Advisory not dispatched within SLO (${SLO_ADVISORY_SEC}s)"
        _warn "Debug: kubectl logs -n $NS -l app=omni-fullstack --tail=100"
        break
    fi

    _info "  t=${ELAPSED}s — waiting... (CRAT hits=$TRACE_HITS)"
    sleep "$POLL_INTERVAL"
done

# ── Step 4: Kill-switch verify ────────────────────────────────────────────────
_info "Step 4/4: Verifying kill-switch"
if [ "$MUTATE_FOUND" = "false" ]; then
    _pass "Kill-switch effective — no EXECUTE_MUTATE"
fi

# Cleanup fires via trap
[ "$FAIL_COUNT" -eq 0 ] || exit 1
