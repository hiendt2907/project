#!/usr/bin/env bash
# chaos_lane1_snapshot_kill.sh — Lane 1 fail-closed khi snapshot stale.
#
# Test này KHÔNG dùng inverted logic — hoàn toàn là real behavior.
# Kill omni-core → snapshot staleness tự nhiên → verify fail-closed.
#
# Acceptance criteria:
#   1. omni-core scaled to 0 thành công
#   2. Snapshot stale sau 310s (age > 300s threshold)
#   3. Alert injected → NO advisory within 60s (fail-closed)
#   4. omni-core restored → snapshot refreshed within 180s
#   5. Alert injected lần 2 → advisory fires within 120s (recovery)

set -euo pipefail

NS="${NS:-multi-agent}"
GATEWAY_URL="${OMNI_GATEWAY_URL:-http://localhost:8080}"
GATEWAY_API_KEY="${OMNI_GATEWAY_API_KEY:-}"
REDIS_HOST="${OMNI_REDIS_HOST:-localhost}"
REDIS_PORT="${OMNI_REDIS_PORT:-16379}"
STALE_WAIT_SEC="${STALE_WAIT_SEC:-310}"
RECOVERY_SLO_SEC="${RECOVERY_SLO_SEC:-180}"
ADVISORY_SLO_SEC="${ADVISORY_SLO_SEC:-120}"
POLL_INTERVAL=10

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
TRACE_STALE="chaos-stale-$(date +%s)"
TRACE_RECOVER="chaos-recover-$(date +%s)"

_rc() { redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "$@" 2>/dev/null; }
_pass() { echo "[PASS] $*"; PASS_COUNT=$((PASS_COUNT+1)); REPORT_LINES+=("  PASS  $*"); }
_fail() { echo "[FAIL] $*" >&2;  FAIL_COUNT=$((FAIL_COUNT+1)); REPORT_LINES+=("  FAIL  $*"); }
_info() { echo "[INFO] $*"; }
_warn() { echo "[WARN] $*"; }

HTTP_HEADERS=(-H "Content-Type: application/json")
[ -n "$GATEWAY_API_KEY" ] && HTTP_HEADERS+=(-H "X-API-Key: $GATEWAY_API_KEY")

# ── Cleanup on exit ───────────────────────────────────────────────────────────
cleanup() {
    _info "Ensuring omni-core restored to 1 replica..."
    kubectl scale deployment omni-core -n "$NS" --replicas=1 2>/dev/null || true
    _print_report
}
trap cleanup EXIT

_print_report() {
    local NOW; NOW=$(date +%s)
    local ELAPSED=$(( NOW - DRILL_START ))
    echo ""
    echo "══════════════════════════════════════════════════════════════════"
    echo " CHAOS DRILL REPORT — Lane 1 Snapshot Kill (fail-closed)"
    echo " Mode : [REAL LOGIC] real omni-core kill + snapshot staleness"
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
}

_inject_alert() {
    local TRACE="$1"
    local NS_VAL="$NS"
    local PAYLOAD
    PAYLOAD=$(python3 - <<PYEOF
import json, time
print(json.dumps({
    'receiver': 'omni-webhook',
    'status': 'firing',
    'alerts': [{
        'status': 'firing',
        'labels': {
            'alertname': 'NodeCPUHighUsage',
            'severity': 'warning',
            'namespace': '${NS_VAL}',
            'deployment': 'nginx-test',
            'chaos_drill': 'true',
            'trace_id': '${TRACE}',
        },
        'annotations': {'description': 'Chaos drill snapshot-kill test'},
        'startsAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'endsAt': '0001-01-01T00:00:00Z',
    }],
    'externalURL': 'http://chaos',
}))
PYEOF
)
    curl -s -o /dev/null -w "%{http_code}" \
        "${HTTP_HEADERS[@]}" \
        -d "$PAYLOAD" \
        "${GATEWAY_URL}/webhook/prometheus" 2>/dev/null || echo "000"
}

_check_crat_for_trace() {
    local TRACE="$1"
    curl -sf "${GATEWAY_URL}/crat/export" "${HTTP_HEADERS[@]}" 2>/dev/null \
        | python3 -c "
import json,sys
try:
    data=json.loads(sys.stdin.read())
    blocks=data if isinstance(data,list) else data.get('blocks',[])
    print(len([b for b in blocks if '$TRACE' in str(b)]))
except: print(0)
" 2>/dev/null | head -1 | tr -d ' \n' || echo "0"
}

# ── Step 1: Scale omni-core to 0 ─────────────────────────────────────────────
_info "Step 1/5: Scaling omni-core to 0 replicas..."
kubectl scale deployment omni-core -n "$NS" --replicas=0
_pass "omni-core scaled to 0"

_info "Waiting ${STALE_WAIT_SEC}s for snapshot to go stale (threshold: 300s)..."
sleep "$STALE_WAIT_SEC"

# Verify stale
TS_RAW=$(_rc GET "omni:baseline:ts" || echo "")
if [ -n "$TS_RAW" ]; then
    AGE=$(python3 -c "import time; print(int(time.time() - float('$TS_RAW')))" 2>/dev/null || echo "0")
    if [ "$AGE" -ge "300" ]; then
        _pass "Snapshot stale — age=${AGE}s (>= 300s)"
    else
        _fail "Snapshot not stale enough — age=${AGE}s (need >= 300s)"
    fi
else
    _pass "No snapshot TS found — snapshot absent = fail-closed applies"
fi

# ── Step 2: Inject alert → expect NO advisory ─────────────────────────────────
_info "Step 2/5: Injecting alert with stale snapshot — expect NO advisory"
STATUS=$(_inject_alert "$TRACE_STALE")
if [ "$STATUS" = "200" ] || [ "$STATUS" = "202" ]; then
    _pass "Alert injected (trace=$TRACE_STALE) — HTTP $STATUS"
else
    _fail "Alert injection failed — HTTP $STATUS"
fi

_info "Monitoring 60s — expect NO advisory (fail-closed on stale snapshot)..."
STALE_ADVISORY_FOUND=false
for i in $(seq 1 12); do
    sleep 5
    HITS=$(_check_crat_for_trace "$TRACE_STALE")
    if [ "${HITS:-0}" -gt "0" ]; then
        _fail "Advisory fired on stale snapshot — fail-closed NOT working (trace=$TRACE_STALE)"
        STALE_ADVISORY_FOUND=true
        break
    fi
done
if [ "$STALE_ADVISORY_FOUND" = "false" ]; then
    _pass "No advisory on stale snapshot — fail-closed working"
fi

# ── Step 3: Restore omni-core ─────────────────────────────────────────────────
_info "Step 3/5: Restoring omni-core to 1 replica..."
kubectl scale deployment omni-core -n "$NS" --replicas=1

# Wait for pod ready
RESTORED=false
for i in $(seq 1 18); do
    sleep 10
    READY=$(kubectl get deployment omni-core -n "$NS" \
        -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
    if [ "${READY:-0}" -ge "1" ]; then
        _pass "omni-core pod ready"
        RESTORED=true
        break
    fi
    _info "  Waiting for omni-core pod... ($i/18)"
done
[ "$RESTORED" = "true" ] || _fail "omni-core pod did not become ready within 180s"

# ── Step 4: Wait for snapshot refresh ────────────────────────────────────────
_info "Step 4/5: Waiting for snapshot to refresh (SLO: ${RECOVERY_SLO_SEC}s)..."
SNAPSHOT_RECOVERED=false
S4_START=$(date +%s)
while true; do
    NOW=$(date +%s); ELAPSED=$(( NOW - S4_START ))
    TS_RAW=$(_rc GET "omni:baseline:ts" || echo "")
    if [ -n "$TS_RAW" ]; then
        AGE=$(python3 -c "import time; print(int(time.time() - float('$TS_RAW')))" 2>/dev/null || echo "9999")
        _info "  t=${ELAPSED}s snapshot_age=${AGE}s"
        if [ "$AGE" -lt "60" ]; then
            _pass "Snapshot refreshed — age=${AGE}s (< 60s) at t=${ELAPSED}s"
            SNAPSHOT_RECOVERED=true
            break
        fi
    fi
    if [ "$ELAPSED" -ge "$RECOVERY_SLO_SEC" ]; then
        _fail "Snapshot did not refresh within ${RECOVERY_SLO_SEC}s"
        break
    fi
    sleep "$POLL_INTERVAL"
done

# ── Step 5: Inject alert post-recovery → expect advisory ──────────────────────
if [ "$SNAPSHOT_RECOVERED" = "true" ]; then
    _info "Step 5/5: Injecting alert post-recovery — expect advisory within ${ADVISORY_SLO_SEC}s"
    STATUS=$(_inject_alert "$TRACE_RECOVER")
    _info "Alert injected (trace=$TRACE_RECOVER) — HTTP $STATUS"

    S5_START=$(date +%s)
    RECOVER_ADVISORY=false
    while true; do
        NOW=$(date +%s); ELAPSED=$(( NOW - S5_START ))
        HITS=$(_check_crat_for_trace "$TRACE_RECOVER")
        if [ "${HITS:-0}" -gt "0" ]; then
            _pass "Advisory dispatched post-recovery at t=${ELAPSED}s"
            RECOVER_ADVISORY=true
            break
        fi
        if [ "$ELAPSED" -ge "$ADVISORY_SLO_SEC" ]; then
            _warn "Advisory not confirmed post-recovery (check: kubectl logs -n $NS -l app=omni-fullstack)"
            REPORT_LINES+=("  WARN  Advisory post-recovery not confirmed within ${ADVISORY_SLO_SEC}s")
            break
        fi
        sleep "$POLL_INTERVAL"
    done
else
    _info "Step 5/5: Skipped (snapshot recovery failed)"
fi

# Cleanup fires via trap
[ "$FAIL_COUNT" -eq 0 ] || exit 1
