#!/usr/bin/env bash
# POST alert Alertmanager (pod+namespace) vào gateway → trace_id → logs pod + Loki.
#
# Usage: scripts/gateway_alert_loki_verify.sh [path/to/alert.json]
# Env: LOKI_URL=http://loki.monitor.svc.cluster.local:3100  SLEEP_SEC=25
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
PAYLOAD="${1:-${ROOT}/scripts/alert_payloads/alertmanager_business_sane.json}"
GW_INTERNAL="${GW_INTERNAL:-http://omni-gateway.multi-agent.svc.cluster.local}"
LOKI_URL="${LOKI_URL:-http://loki.monitor.svc.cluster.local:3100}"
SLEEP_SEC="${SLEEP_SEC:-25}"

if [[ ! -f "$PAYLOAD" ]]; then
  echo "Missing payload: $PAYLOAD" >&2
  exit 1
fi

echo "=== 1) POST ${GW_INTERNAL}/webhook/prometheus ==="
RESP="$("${KUBE}" exec -i -n multi-agent deploy/omni-worker -- python3 -c "
import json, sys, urllib.request
body = sys.stdin.buffer.read()
req = urllib.request.Request(
    '${GW_INTERNAL}/webhook/prometheus',
    data=body,
    headers={'Content-Type': 'application/json'},
    method='POST',
)
print(urllib.request.urlopen(req, timeout=45).read().decode())
" < "$PAYLOAD")"

TRACE="$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('trace_id',''))" 2>/dev/null || true)"
if [[ -z "$TRACE" ]]; then
  echo "Response: $RESP" >&2
  echo "FAIL: no trace_id (circuit breaker / redis / gateway error)." >&2
  exit 1
fi
echo "trace_id=$TRACE"
echo ""

echo "=== 2) omni-gateway logs (enqueue) ==="
"${KUBE}" logs -n multi-agent deploy/omni-gateway --tail=200 2>/dev/null | grep -F "$TRACE" || echo "(no line — tail nhỏ hoặc replica khác)"

echo ""
echo "=== 3) Chờ worker xử lý (${SLEEP_SEC}s) rồi omni-worker logs (since 15m) ==="
sleep "$SLEEP_SEC"
WR_LINES="$("${KUBE}" logs -n multi-agent deploy/omni-worker --since=15m --tail=4000 2>/dev/null | grep -F "$TRACE" || true)"
if [[ -z "$WR_LINES" ]]; then
  echo "(no worker lines — tăng SLEEP_SEC / backlog events:inbound)"
else
  echo "$WR_LINES" | tail -120
  echo ""
  echo "--- grep handler_done (gợi ý kết quả nghiệp vụ) ---"
  echo "$WR_LINES" | grep -F "handler_done" || echo "(chưa có handler_done — worker còn xử lý hoặc lỗi)"
fi

echo ""
echo "=== 4) Loki query_range (Promtail: namespace + pod_name) ==="
echo "LogQL (Grafana Explore): {namespace=\"multi-agent\", pod_name=~\"omni-worker.*\"} |= \"$TRACE\""
"${KUBE}" exec -n multi-agent deploy/omni-worker -- python3 -c "
import json, time, urllib.parse, urllib.request
trace = '''${TRACE}'''
loki = '''${LOKI_URL}'''
# Promtail: label pod_name (không phải pod)
q = '{namespace=\"multi-agent\", pod_name=~\"omni-worker.*|omni-gateway.*\"} |= \"' + trace + '\"'
now = int(time.time())
start = (now - 3600) * 10**9
end = now * 10**9
params = urllib.parse.urlencode({'query': q, 'limit': '100', 'start': str(start), 'end': str(end)})
url = loki.rstrip('/') + '/loki/api/v1/query_range?' + params
try:
    r = urllib.request.urlopen(url, timeout=25)
    d = json.loads(r.read().decode())
except Exception as e:
    print(json.dumps({'loki_error': str(e)}))
    raise SystemExit(0)
res = d.get('data', {}).get('result') or []
lines = []
for s in res:
    for v in s.get('values') or []:
        if len(v) >= 2:
            lines.append(v[1])
print('--- Loki (last 35 lines) ---')
for ln in lines[-35:]:
    print((ln[:600] + '…') if len(ln) > 600 else ln)
if not lines:
    print('(empty — kiểm tra Promtail ship namespace multi-agent / Loki DNS)')
"

echo ""
echo "=== 5) Checklist nghiệp vụ ==="
echo "• Inbound phải có pod= + namespace= trong FACTS (alert JSON có labels)."
echo "• Không kỳ vọng business_done nếu chỉ thấy escalate_to_human lượt 1 (đã chặn = escalate_blocked + tool discovery)."
echo "• Dùng trace trong Grafana Explore Loki:  $TRACE"
