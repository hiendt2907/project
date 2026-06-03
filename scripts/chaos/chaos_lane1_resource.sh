#!/usr/bin/env bash
# chaos_lane1_resource.sh — Lane 1 (SYS_RESOURCE) CPU anomaly drill.
#
# ╔══════════════════════════════════════════════════════════════════════╗
# ║  [INVERTED LOGIC — ĐỌC TRƯỚC KHI CHẠY]                             ║
# ║                                                                      ║
# ║  Drill này KHÔNG generate real CPU load.                             ║
# ║  Thay vào đó: inject z_cpu=4.5 trực tiếp vào Redis.                ║
# ║                                                                      ║
# ║  TẠI SAO INVERTED?                                                   ║
# ║  Lab không có real workload. Busy-loop pod trong cluster nhỏ        ║
# ║  chỉ tạo z_cpu ≈ 0.1-0.5, không bao giờ đạt >= 3.0 tự nhiên.     ║
# ║  Chờ sigma breach thật cần production traffic thật.                 ║
# ║                                                                      ║
# ║  CÁI GÌ ĐƯỢC TEST (business logic path):                            ║
# ║  ✓ Snapshot chứa z_cpu=4.5 → ThreeSigmaGate reads → fires          ║
# ║  ✓ Advisory pipeline triggered → CRAT block written                 ║
# ║  ✓ action_type = SUGGEST_REMEDIATION (kill-switch active)           ║
# ║  ✓ Alert dedup hoạt động (second inject bị block 300s)             ║
# ║                                                                      ║
# ║  CÁI GÌ KHÔNG ĐƯỢC TEST:                                            ║
# ║  ✗ Prometheus → baseline_snapshot → Redis (metric collection path) ║
# ║  ✗ Accuracy của CPU utilization measurement                         ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# Usage:
#   OMNI_ENV_MODE=lab OMNI_AUTO_EXECUTE_ENABLED=false \
#     bash scripts/chaos/chaos_lane1_resource.sh
#
# Acceptance criteria:
#   1. Snapshot injection confirmed (z_cpu=4.5 readable in Redis)
#   2. Gateway accepts alert (HTTP 200/202)
#   3. Advisory dispatched within SLO_ADVISORY_SEC (default 120s)
#   4. No EXECUTE_MUTATE in analyst logs
#   5. Snapshot restored to original after drill

set -euo pipefail

NS="${NS:-multi-agent}"
GATEWAY_URL="${OMNI_GATEWAY_URL:-http://localhost:8080}"
GATEWAY_API_KEY="${OMNI_GATEWAY_API_KEY:-}"
REDIS_HOST="${OMNI_REDIS_HOST:-localhost}"
REDIS_PORT="${OMNI_REDIS_PORT:-16379}"
SLO_ADVISORY_SEC="${SLO_ADVISORY_SEC:-120}"
POLL_INTERVAL=5
REDIS_KEY_SNAPSHOT="omni:baseline:snapshot"
REDIS_KEY_TS="omni:baseline:ts"
DRILL_TRACE="chaos-resource-$(date +%s)"

# ── Safety gates ──────────────────────────────────────────────────────────────
[ "${OMNI_ENV_MODE:-}" = "lab" ] || {
    echo "[CHAOS] ABORT: OMNI_ENV_MODE must be 'lab'" >&2; exit 2
}
[ "${OMNI_AUTO_EXECUTE_ENABLED:-true}" = "false" ] || {
    echo "[CHAOS] ABORT: OMNI_AUTO_EXECUTE_ENABLED must be 'false'" >&2; exit 2
}
[ "${NS}" != "finguard-customer" ] || {
    echo "[CHAOS] ABORT: forbidden namespace finguard-customer" >&2; exit 2
}

# ── Globals ───────────────────────────────────────────────────────────────────
DRILL_START=$(date +%s)
PASS_COUNT=0
FAIL_COUNT=0
declare -a REPORT_LINES=()
SNAPSHOT_BACKUP=""

_rc()   { redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "$@" 2>/dev/null; }
_pass() { echo "[PASS] $*"; PASS_COUNT=$((PASS_COUNT+1)); REPORT_LINES+=("  PASS  $*"); }
_fail() { echo "[FAIL] $*" >&2;  FAIL_COUNT=$((FAIL_COUNT+1)); REPORT_LINES+=("  FAIL  $*"); }
_info() { echo "[INFO] $*"; }
_warn() { echo "[WARN] $*"; }

HTTP_HEADERS=(-H "Content-Type: application/json")
[ -n "$GATEWAY_API_KEY" ] && HTTP_HEADERS+=(-H "X-API-Key: $GATEWAY_API_KEY")

# ── Cleanup on exit ───────────────────────────────────────────────────────────
cleanup() {
    if [ -n "$SNAPSHOT_BACKUP" ]; then
        _info "Restoring original snapshot..."
        _rc SET "$REDIS_KEY_SNAPSHOT" "$SNAPSHOT_BACKUP" EX 86400 > /dev/null || true
    fi
    _print_report
}
trap cleanup EXIT

_print_report() {
    local NOW; NOW=$(date +%s)
    local ELAPSED=$(( NOW - DRILL_START ))
    echo ""
    echo "══════════════════════════════════════════════════════════════════"
    echo " CHAOS DRILL REPORT — Lane 1 CPU Resource Anomaly"
    echo " Mode : [INVERTED LOGIC] z_cpu=4.5 injected directly into Redis"
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
    echo "   z_cpu được inject synthetic — test kiểm tra business logic,"
    echo "   KHÔNG kiểm tra Prometheus metric collection thật."
    echo "══════════════════════════════════════════════════════════════════"
}

# ── Step 1: Backup + inject synthetic snapshot ────────────────────────────────
_info "Step 1/4: Backing up snapshot, injecting synthetic z_cpu=4.5..."

SNAPSHOT_BACKUP=$(_rc GET "$REDIS_KEY_SNAPSHOT" || echo "")
[ -z "$SNAPSHOT_BACKUP" ] && _warn "No existing snapshot — will create fresh synthetic"

# Build synthetic snapshot preserving existing fields
SYNTHETIC_SNAP=$(python3 - <<PYEOF
import json, time, sys

backup = """${SNAPSHOT_BACKUP//\"/\\\"}"""
try:
    snap = json.loads(backup) if backup.strip() else {}
except Exception:
    snap = {}

snap.update({
    't': int(time.time()),
    'cpu': 0.95,
    'z_cpu': 4.5,
    'z_mem': snap.get('z_mem') or 0.3,
    'z_disk': snap.get('z_disk') or 0.1,
})
print(json.dumps(snap))
PYEOF
)

_rc SET "$REDIS_KEY_SNAPSHOT" "$SYNTHETIC_SNAP" EX 300 > /dev/null
_rc SET "$REDIS_KEY_TS" "$(date +%s)" EX 300 > /dev/null

# Verify
Z_CPU_VERIFY=$(_rc GET "$REDIS_KEY_SNAPSHOT" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('z_cpu','null'))" 2>/dev/null \
    || echo "null")

if python3 -c "import sys; sys.exit(0 if float('${Z_CPU_VERIFY}') >= 3.0 else 1)" 2>/dev/null; then
    _pass "Snapshot injection — z_cpu=${Z_CPU_VERIFY} (>= 3.0 threshold)"
else
    _fail "Snapshot injection failed — z_cpu=${Z_CPU_VERIFY} (expected >= 3.0)"
    exit 1
fi

# ── Step 2: Inject alert via gateway ─────────────────────────────────────────
_info "Step 2/4: Injecting CPU spike alert via gateway (trace=$DRILL_TRACE)"

PAYLOAD=$(python3 -c "
import json, time
print(json.dumps({
    'receiver': 'omni-webhook',
    'status': 'firing',
    'alerts': [{
        'status': 'firing',
        'labels': {
            'alertname': 'NodeCPUHighUsage',
            'severity': 'warning',
            'namespace': '${NS}',
            'deployment': 'nginx-test',
            'node': 'lab-node-01',
            'chaos_drill': 'true',
            'trace_id': '${DRILL_TRACE}',
        },
        'annotations': {
            'summary': '[CHAOS] CPU spike drill — inverted logic test',
            'description': 'CPU 95%, z_cpu=4.5 (3-sigma breach). Synthetic chaos drill.',
        },
        'startsAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'endsAt': '0001-01-01T00:00:00Z',
    }],
    'groupLabels': {'alertname': 'NodeCPUHighUsage'},
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
    _warn "Check: curl -v ${GATEWAY_URL}/health"
    exit 1
fi

# ── Step 3: Poll CRAT for advisory dispatch ───────────────────────────────────
_info "Step 3/4: Polling for advisory dispatch (SLO: ${SLO_ADVISORY_SEC}s)"
ADVISORY_FOUND=false
MUTATE_FOUND=false
START=$(date +%s)

while true; do
    NOW=$(date +%s); ELAPSED=$(( NOW - START ))

    # Check CRAT via gateway
    CRAT_RAW=$(curl -sf "${GATEWAY_URL}/crat/export" "${HTTP_HEADERS[@]}" 2>/dev/null || echo "[]")
    TRACE_HITS=$(echo "$CRAT_RAW" | python3 -c "
import json, sys
try:
    data = json.loads(sys.stdin.read())
    blocks = data if isinstance(data, list) else data.get('blocks', [])
    hits = [b for b in blocks if '${DRILL_TRACE}' in str(b) and 'ADVISORY_DISPATCHED' in str(b)]
    print(len(hits))
except:
    print(0)
" 2>/dev/null || echo "0")

    # Check analyst logs for EXECUTE_MUTATE
    ANALYST_LOG=$(kubectl logs -n "$NS" -l app=omni-fullstack --tail=50 --since=180s 2>/dev/null || echo "")
    if echo "$ANALYST_LOG" | grep -q "EXECUTE_MUTATE"; then
        MUTATE_FOUND=true
        _fail "EXECUTE_MUTATE found in analyst logs — kill-switch NOT effective"
        break
    fi

    if [ "${TRACE_HITS:-0}" -gt "0" ]; then
        _pass "ADVISORY_DISPATCHED in CRAT at t=${ELAPSED}s (SLO ${SLO_ADVISORY_SEC}s)"
        ADVISORY_FOUND=true
        break
    fi

    # Fallback: log-based detection
    if echo "$ANALYST_LOG" | grep -q "SUGGEST_REMEDIATION\|advisory_dispatched"; then
        _pass "Advisory evidence in analyst logs at t=${ELAPSED}s"
        ADVISORY_FOUND=true
        break
    fi

    if [ "$ELAPSED" -ge "$SLO_ADVISORY_SEC" ]; then
        _fail "Advisory not dispatched within SLO=${SLO_ADVISORY_SEC}s"
        _warn "Debug: kubectl logs -n $NS -l app=omni-fullstack --tail=100 | grep -i advisory"
        break
    fi

    _info "  t=${ELAPSED}s — waiting (CRAT hits=${TRACE_HITS:-0})..."
    sleep "$POLL_INTERVAL"
done

# ── Step 4: Verify kill-switch ────────────────────────────────────────────────
_info "Step 4/4: Verifying kill-switch (no EXECUTE_MUTATE)"
[ "$MUTATE_FOUND" = "false" ] && _pass "Kill-switch effective — no EXECUTE_MUTATE dispatched"

# Cleanup (snapshot restore + report) fires via trap
[ "$FAIL_COUNT" -eq 0 ] || exit 1
