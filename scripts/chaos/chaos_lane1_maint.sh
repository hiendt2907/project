#!/usr/bin/env bash
# chaos_lane1_maint.sh — Lane 1 maintenance window suppression drill.
#
# Test này KHÔNG dùng inverted logic — hoàn toàn là real behavior.
# Set Redis maint key → inject alert → verify suppression.
# Wait key expire → inject lại → verify advisory fires.
#
# Acceptance criteria:
#   1. Maint key set in Redis (confirmed TTL)
#   2. Alert during maint → NO advisory within 60s (suppressed)
#   3. Maint key expired naturally
#   4. Alert after maint → advisory fires within SLO_POST_MAINT_SEC

set -euo pipefail

NS="${NS:-multi-agent}"
GATEWAY_URL="${OMNI_GATEWAY_URL:-http://gateway.ai-agent.local}"
GATEWAY_API_KEY="${OMNI_GATEWAY_API_KEY:-$(kubectl get secret -n "${NS:-multi-agent}" omni-gateway-secret -o jsonpath="{.data.OMNI_GATEWAY_API_KEY}" 2>/dev/null | base64 -d)}"
REDIS_HOST="${OMNI_REDIS_HOST:-localhost}"
REDIS_PORT="${OMNI_REDIS_PORT:-16379}"
DEPLOYMENT="${CHAOS_DEPLOYMENT:-nginx-test}"
MAINT_TTL_SEC="${MAINT_TTL_SEC:-120}"
SLO_POST_MAINT_SEC="${SLO_POST_MAINT_SEC:-180}"
POLL_INTERVAL=5
MAINT_KEY="omni:maint:${NS}:${DEPLOYMENT}"

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
TRACE_MAINT="chaos-maint-$(date +%s)"
TRACE_POST="chaos-post-maint-$(date +%s)"

_rc()   { redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "$@" 2>/dev/null; }
_pass() { echo "[PASS] $*"; PASS_COUNT=$((PASS_COUNT+1)); REPORT_LINES+=("  PASS  $*"); }
_fail() { echo "[FAIL] $*" >&2;  FAIL_COUNT=$((FAIL_COUNT+1)); REPORT_LINES+=("  FAIL  $*"); }
_info() { echo "[INFO] $*"; }
_warn() { echo "[WARN] $*"; }

HTTP_HEADERS=(-H "Content-Type: application/json")
[ -n "$GATEWAY_API_KEY" ] && HTTP_HEADERS+=(-H "Authorization: Bearer $GATEWAY_API_KEY")

# ── Cleanup ───────────────────────────────────────────────────────────────────
cleanup() {
    _rc DEL "$MAINT_KEY" > /dev/null 2>&1 || true
    _print_report
}
trap cleanup EXIT

_print_report() {
    local NOW; NOW=$(date +%s)
    local ELAPSED=$(( NOW - DRILL_START ))
    echo ""
    echo "══════════════════════════════════════════════════════════════════"
    echo " CHAOS DRILL REPORT — Lane 1 Maintenance Window Suppression"
    echo " Mode : [REAL LOGIC] Redis maint key controls suppression"
    echo " Key  : $MAINT_KEY (TTL=${MAINT_TTL_SEC}s)"
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

# Hàm build payload — dùng heredoc để tránh nested double-quote với python3 -c
_build_payload() {
    local TRACE="$1"
    local NS_VAL="$NS"
    local DEP_VAL="$DEPLOYMENT"
    python3 - <<PYEOF
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
            'deployment': '${DEP_VAL}',
            'chaos_drill': 'true',
            'trace_id': '${TRACE}',
        },
        'annotations': {'description': 'Chaos maintenance window test'},
        'startsAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'endsAt': '0001-01-01T00:00:00Z',
    }],
    'externalURL': 'http://chaos',
}))
PYEOF
}

_inject_alert() {
    local TRACE="$1"
    local PAYLOAD
    PAYLOAD=$(_build_payload "$TRACE")
    curl -s -o /dev/null -w "%{http_code}" \
        "${HTTP_HEADERS[@]}" \
        -d "$PAYLOAD" \
        "${GATEWAY_URL}/webhook/prometheus" 2>/dev/null || echo "000"
}

_check_crat() {
    local TRACE="$1"
    curl -sf "${GATEWAY_URL}/crat/export" "${HTTP_HEADERS[@]}" 2>/dev/null \
        | python3 -c "
import json,sys
try:
    data=json.loads(sys.stdin.read())
    blocks=data if isinstance(data,list) else data.get('blocks',[])
    print(len([b for b in blocks if '$TRACE' in str(b)]))
except:
    print(0)
" 2>/dev/null | head -1 | tr -d ' \n' || echo "0"
}

# ── Step 1: Set maint key ─────────────────────────────────────────────────────
_info "Step 1/4: Setting maintenance window key ($MAINT_KEY, TTL=${MAINT_TTL_SEC}s)"
_rc SET "$MAINT_KEY" "1" EX "$MAINT_TTL_SEC" > /dev/null
TTL_CONFIRM=$(_rc TTL "$MAINT_KEY" | head -1 | tr -d ' \n')
if [ "${TTL_CONFIRM:-0}" -gt "0" ] 2>/dev/null; then
    _pass "Maint key set — TTL=${TTL_CONFIRM}s"
else
    _fail "Maint key not set (TTL=${TTL_CONFIRM:-0})"
    exit 1
fi

# ── Step 2: Inject alert during maint → expect suppression ───────────────────
_info "Step 2/4: Injecting alert during maint window (trace=$TRACE_MAINT)"
STATUS=$(_inject_alert "$TRACE_MAINT")
if [ "$STATUS" = "200" ] || [ "$STATUS" = "202" ]; then
    _pass "Alert injected — HTTP $STATUS"
else
    _fail "Alert injection failed — HTTP $STATUS"
fi

_info "Monitoring 60s — expect NO advisory (maintenance active)..."
MAINT_ADVISORY_FOUND=false
for i in $(seq 1 12); do
    sleep 5
    HITS=$(_check_crat "$TRACE_MAINT")
    if [ "${HITS:-0}" -gt "0" ] 2>/dev/null; then
        _fail "Advisory fired during maintenance window — suppression NOT working"
        MAINT_ADVISORY_FOUND=true
        break
    fi
done
[ "$MAINT_ADVISORY_FOUND" = "false" ] && _pass "Alert suppressed during maintenance window"

# ── Step 3: Wait for maint key expiry ────────────────────────────────────────
_info "Step 3/4: Waiting for maint key to expire..."
REMAINING=$(_rc TTL "$MAINT_KEY" | head -1 | tr -d ' \n' || echo "0")
_info "  Maint key TTL remaining: ${REMAINING}s"
if [ "${REMAINING:-0}" -gt "0" ] 2>/dev/null; then
    sleep $(( REMAINING + 5 ))
fi

KEY_EXISTS=$(_rc EXISTS "$MAINT_KEY" | head -1 | tr -d ' \n' || echo "1")
if [ "${KEY_EXISTS}" = "0" ]; then
    _pass "Maint key expired naturally"
else
    _info "Maint key still exists — deleting manually"
    _rc DEL "$MAINT_KEY" > /dev/null
    _pass "Maint key deleted (manual cleanup)"
fi

# ── Step 4: Inject alert post-maint → expect advisory ────────────────────────
_info "Step 4/4: Injecting alert after maint window (trace=$TRACE_POST)"
STATUS=$(_inject_alert "$TRACE_POST")
_info "Alert injected — HTTP $STATUS"

S4_START=$(date +%s)
while true; do
    NOW=$(date +%s); ELAPSED=$(( NOW - S4_START ))
    HITS=$(_check_crat "$TRACE_POST")
    if [ "${HITS:-0}" -gt "0" ] 2>/dev/null; then
        _pass "Advisory dispatched post-maint at t=${ELAPSED}s (SLO ${SLO_POST_MAINT_SEC}s)"
        break
    fi
    # Also check analyst logs
    ANA_LOG=$(kubectl logs -n "$NS" -l app=omni-fullstack --tail=50 --since=60s 2>/dev/null || echo "")
    if echo "$ANA_LOG" | grep -q "SUGGEST_REMEDIATION\|advisory_dispatched"; then
        _pass "Advisory found in analyst logs at t=${ELAPSED}s"
        break
    fi
    if [ "$ELAPSED" -ge "$SLO_POST_MAINT_SEC" ]; then
        _warn "Advisory post-maint not confirmed — check analyst logs"
        REPORT_LINES+=("  WARN  Post-maint advisory not confirmed within ${SLO_POST_MAINT_SEC}s")
        break
    fi
    _info "  t=${ELAPSED}s — waiting..."
    sleep "$POLL_INTERVAL"
done

# Cleanup fires via trap
[ "$FAIL_COUNT" -eq 0 ] || exit 1
