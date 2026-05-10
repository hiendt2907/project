#!/usr/bin/env bash
# POST alert Alertmanager (pod+namespace) vào gateway → trace_id → logs pod + Loki.
#
# Master Plan V3 (split): exec mặc định deploy/omni-prober (consumer omni-alerts — đúng luồng Alert).
# Không dùng omni-worker khi replicas=0; tùy chọn debug: E2E_EXEC_DEPLOY=omni-core (cùng image worker).
# Gom log: omni-prober / omni-analyst / omni-core / omni-executor (+ omni-worker nếu scale > 0).
#
# Usage: NS=<ns> scripts/gateway_alert_loki_verify.sh [path/to/alert.json]
# Default payload: nginx-test HighCPU ~90% — not redis probe lab.
# Env:
#   NS=                       **required** — Kubernetes namespace for omni workloads / alert labels
#   LOKI_URL=http://loki.monitor.svc.cluster.local:3100
#   SLEEP_SEC=25
#   E2E_EXEC_DEPLOY=omni-prober          # Pod chạy python3 để POST tới gateway
#   E2E_TRACE_LOG_DEPLOYS="omni-prober …" # override danh sách grep log theo trace
#   E2E_NGINX_POD_AUTO=1                 # (default) với alertmanager_nginx_cpu_high.json: patch labels.pod = pod app=nginx-test hiện tại
#   E2E_REDIS_POD_AUTO=1                 # (default) với alertmanager_business_sane.json: patch labels.pod = live app=redis-exporter
#   STRICT_ASSERT=1                     # trace in >=N worker deploy logs + action marker (N=STRICT_ASSERT_MIN_DEPLOY_HITS, default 3)
#   STRICT_ASSERT_MIN_DEPLOY_HITS=3
#   STRICT_ASSERT_INCLUDE_ADVISORY_MARKERS=0  # if 1, stage marker grep also accepts advisory/CRAT logs (advisory_analyst_ok|ADVISORY_DECISION|…)
#   E2E_ASSERT_TELEGRAM_BOT_API=1       # sau Loki: assert tin advisory qua Telegram getUpdates (cần TELEGRAM_BOT_TOKEN; nên OMNI_TELEGRAM_POLLING_ENABLED=false trên prober)
#   E2E_ASSERT_DIAGNOSTIC_POLICY=1        # optional: INV_/DIAGNOSTIC_* or agentic/discovery (agentic_mutate_plan|readonly_discovery_redirect|k8s_args_coerced); SLEEP_SEC>=120 if Ollama slow
#   E2E_EXTRA_AGENTIC_SLEEP=0           # After first SLEEP_SEC, wait N more seconds then re-grep logs (agentic loop: first LLM step can be 60–120s after PLAN_EMITTED; multi-step needs longer). Use with waiting_fault / broken_spec (e.g. 180–300).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
PAYLOAD="${1:-${ROOT}/scripts/alert_payloads/alertmanager_nginx_cpu_high.json}"
PAYLOAD_SRC="$PAYLOAD"
PATCHED_PAYLOAD=""
PATCHED_PAYLOAD_REDIS=""
_E2E_NS_PATCH=""
GW_INTERNAL="${GW_INTERNAL:-}"
LOKI_URL="${LOKI_URL:-http://loki.monitor.svc.cluster.local:3100}"
SLEEP_SEC="${SLEEP_SEC:-25}"
E2E_EXTRA_AGENTIC_SLEEP="${E2E_EXTRA_AGENTIC_SLEEP:-0}"
STRICT_ASSERT="${STRICT_ASSERT:-1}"
STRICT_ASSERT_MIN_DEPLOY_HITS="${STRICT_ASSERT_MIN_DEPLOY_HITS:-3}"

if [[ -z "${NS:-}" ]]; then
  echo "gateway_alert_loki_verify.sh: set NS to the Kubernetes namespace (no default)." >&2
  exit 2
fi
GW_INTERNAL="${GW_INTERNAL:-http://omni-gateway.${NS}.svc.cluster.local}"

# Pod có Python + image worker — tránh omni-gateway (slim) nếu thiếu python.
EXEC_DEPLOY="${E2E_EXEC_DEPLOY:-omni-prober}"

_default_trace_deploys() {
  local base="omni-prober omni-analyst omni-core omni-executor"
  local r
  r="$("${KUBE}" get deploy omni-worker -n "$NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
  if [[ "${r:-0}" != "0" ]]; then
    echo "omni-worker $base"
  else
    echo "$base"
  fi
}

TRACE_LOG_DEPLOYS="${E2E_TRACE_LOG_DEPLOYS:-$(_default_trace_deploys)}"

if [[ ! -f "$PAYLOAD_SRC" ]]; then
  echo "Missing payload: $PAYLOAD_SRC" >&2
  exit 1
fi

_e2e_alert_payload_cleanup() {
  if [[ -n "${_E2E_NS_PATCH:-}" && -f "${_E2E_NS_PATCH}" ]]; then
    rm -f "${_E2E_NS_PATCH}"
  fi
  if [[ -n "${PATCHED_PAYLOAD:-}" && -f "${PATCHED_PAYLOAD}" ]]; then
    rm -f "${PATCHED_PAYLOAD}"
  fi
  if [[ -n "${PATCHED_PAYLOAD_REDIS:-}" && -f "${PATCHED_PAYLOAD_REDIS}" ]]; then
    rm -f "${PATCHED_PAYLOAD_REDIS}"
  fi
}
trap '_e2e_alert_payload_cleanup' EXIT

_E2E_NS_PATCH="$(mktemp "${TMPDIR:-/tmp}/e2e-gw-ns.XXXXXX.json")"
python3 -c "
import json, sys
ns, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src, encoding='utf-8') as f:
    d = json.load(f)
for a in d.get('alerts', []):
    a.setdefault('labels', {})['namespace'] = ns
with open(dst, 'w', encoding='utf-8') as f:
    json.dump(d, f, indent=2)
" "$NS" "$PAYLOAD_SRC" "$_E2E_NS_PATCH"
PAYLOAD="$_E2E_NS_PATCH"

if [[ "${E2E_NGINX_POD_AUTO:-1}" == "1" ]] && [[ "$(basename "$PAYLOAD_SRC")" == "alertmanager_nginx_cpu_high.json" || "$(basename "$PAYLOAD_SRC")" == "alertmanager_nginx_waiting_fault.json" ]]; then
  if [[ -n "${E2E_NGINX_POD:-}" ]]; then
    NGINX_POD="${E2E_NGINX_POD}"
  else
    NGINX_POD="$("${KUBE}" get pods -n "$NS" -l app=nginx-test -o json 2>/dev/null | python3 -c "
import json, sys
items = json.load(sys.stdin).get('items', [])
bad_reasons = frozenset({
    'CreateContainerError', 'CrashLoopBackOff', 'CreateContainerConfigError',
    'ImagePullBackOff', 'ErrImagePull',
})
def pick():
    for it in sorted(items, key=lambda x: x['metadata']['name']):
        for cs in it.get('status', {}).get('containerStatuses') or []:
            r = (cs.get('state') or {}).get('waiting', {}).get('reason') or ''
            if r in bad_reasons:
                return it['metadata']['name']
    for it in sorted(items, key=lambda x: x['metadata']['name']):
        for cs in it.get('status', {}).get('containerStatuses') or []:
            if cs.get('ready') is False:
                return it['metadata']['name']
    return items[0]['metadata']['name'] if items else ''
print(pick())
")"
  fi
  if [[ -z "$NGINX_POD" ]]; then
    echo "FAIL: E2E_NGINX_POD_AUTO: no pod with label app=nginx-test in namespace ${NS}" >&2
    exit 1
  fi
  PATCHED_PAYLOAD="$(mktemp "${TMPDIR:-/tmp}/e2e-gw-alert.XXXXXX.json")"
  python3 -c "
import json, sys
pod, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src) as f:
    d = json.load(f)
for a in d.get('alerts', []):
    if (a.get('labels') or {}).get('deployment') == 'nginx-test':
        a['labels']['pod'] = pod
with open(dst, 'w') as f:
    json.dump(d, f, indent=2)
" "$NGINX_POD" "$PAYLOAD" "$PATCHED_PAYLOAD"
  PAYLOAD="${PATCHED_PAYLOAD}"
  echo "=== 0a) E2E_NGINX_POD_AUTO: alert labels.pod=${NGINX_POD} (live app=nginx-test) ==="
fi

if [[ "${E2E_REDIS_POD_AUTO:-1}" == "1" ]] && [[ "$(basename "$PAYLOAD_SRC")" == "alertmanager_business_sane.json" ]]; then
  if [[ -n "${E2E_REDIS_POD:-}" ]]; then
    REDIS_POD="${E2E_REDIS_POD}"
  else
    REDIS_POD="$("${KUBE}" get pods -n "$NS" -l app=redis-exporter -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  fi
  if [[ -z "$REDIS_POD" ]]; then
    echo "FAIL: E2E_REDIS_POD_AUTO: no pod with label app=redis-exporter in namespace ${NS}" >&2
    exit 1
  fi
  PATCHED_PAYLOAD_REDIS="$(mktemp "${TMPDIR:-/tmp}/e2e-gw-alert-redis.XXXXXX.json")"
  python3 -c "
import json, sys
pod, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src) as f:
    d = json.load(f)
for a in d.get('alerts', []):
    if (a.get('labels') or {}).get('deployment') == 'redis-exporter':
        a['labels']['pod'] = pod
with open(dst, 'w') as f:
    json.dump(d, f, indent=2)
" "$REDIS_POD" "$PAYLOAD" "$PATCHED_PAYLOAD_REDIS"
  PAYLOAD="${PATCHED_PAYLOAD_REDIS}"
  echo "=== 0b) E2E_REDIS_POD_AUTO: alert labels.pod=${REDIS_POD} (live app=redis-exporter) ==="
fi

if ! "${KUBE}" get deploy "$EXEC_DEPLOY" -n "$NS" &>/dev/null; then
  echo "FAIL: no deployment/$EXEC_DEPLOY in $NS (set E2E_EXEC_DEPLOY to a running worker pod, e.g. omni-prober)." >&2
  exit 1
fi
r_exec="$("${KUBE}" get deploy "$EXEC_DEPLOY" -n "$NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
if [[ "${r_exec:-0}" == "0" ]]; then
  echo "FAIL: deployment/$EXEC_DEPLOY has replicas 0 — scale up or set E2E_EXEC_DEPLOY." >&2
  exit 1
fi

echo "=== 0) Topology: exec deploy=$EXEC_DEPLOY | trace logs: $TRACE_LOG_DEPLOYS ==="

echo "=== 1) POST ${GW_INTERNAL}/webhook/prometheus ==="
RESP="$("${KUBE}" exec -i -n "$NS" "deploy/${EXEC_DEPLOY}" -- python3 -c "
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
echo "gateway_json=$RESP"
echo "trace_id=$TRACE"
echo ""

echo "=== 2) omni-gateway logs (enqueue) ==="
"${KUBE}" logs -n "$NS" deploy/omni-gateway --tail=200 2>/dev/null | grep -F "$TRACE" || echo "(no line — tail nhỏ hoặc replica khác)"

echo ""
echo "=== 3) Chờ xử lý (${SLEEP_SEC}s) rồi grep trace trong log split workers ==="
sleep "$SLEEP_SEC"
WR_LINES=""
for dep in $TRACE_LOG_DEPLOYS; do
  r="$("${KUBE}" get deploy "$dep" -n "$NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
  if [[ "${r:-0}" == "0" ]]; then
    continue
  fi
  chunk="$("${KUBE}" logs -n "$NS" "deploy/${dep}" --since=15m --tail=4000 2>/dev/null | grep -F "$TRACE" || true)"
  if [[ -n "$chunk" ]]; then
    WR_LINES+="--- deploy/${dep} ---
${chunk}
"
  fi
done

_collect_trace_logs() {
  WR_LINES=""
  for dep in $TRACE_LOG_DEPLOYS; do
    r="$("${KUBE}" get deploy "$dep" -n "$NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
    if [[ "${r:-0}" == "0" ]]; then
      continue
    fi
    chunk="$("${KUBE}" logs -n "$NS" "deploy/${dep}" --since=15m --tail=8000 2>/dev/null | grep -F "$TRACE" || true)"
    if [[ -n "$chunk" ]]; then
      WR_LINES+="--- deploy/${dep} ---
${chunk}
"
    fi
  done
}

_print_wr_tail() {
  if [[ -z "$WR_LINES" ]]; then
    echo "(no lines in omni-prober/analyst/core/executor[/worker] — tăng SLEEP_SEC hoặc kiểm tra consumer Kafka)"
  else
    echo "$WR_LINES" | tail -n 120
    echo ""
    echo "--- grep handler_done (gợi ý kết quả nghiệp vụ) ---"
    echo "$WR_LINES" | grep -F "handler_done" || echo "(chưa có handler_done — pipeline còn xử lý hoặc chỉ stream_consumer)"
    echo ""
    echo "(Nếu mới chỉ thấy readonly_discovery_redirect step=1: agentic/Ollama còn bước sau — tăng SLEEP_SEC hoặc đặt E2E_EXTRA_AGENTIC_SLEEP=180–300.)"
  fi
}

_print_wr_tail

if [[ "${E2E_EXTRA_AGENTIC_SLEEP}" =~ ^[0-9]+$ ]] && [[ "${E2E_EXTRA_AGENTIC_SLEEP}" -gt 0 ]]; then
  echo ""
  echo "=== 3a) Chờ thêm ${E2E_EXTRA_AGENTIC_SLEEP}s (E2E_EXTRA_AGENTIC_SLEEP) — vòng agentic nhiều bước / Ollama ==="
  sleep "${E2E_EXTRA_AGENTIC_SLEEP}"
  _collect_trace_logs
  echo "--- Log worker sau chờ thêm (tail) ---"
  _print_wr_tail
fi

if [[ "${STRICT_ASSERT}" == "1" ]]; then
  echo ""
  echo "=== 3b) Strict stage assertions ==="
  STAGE_DEP_HITS=0
  for dep in $TRACE_LOG_DEPLOYS; do
    if echo "${WR_LINES}" | grep -qF "deploy/${dep}"; then
      STAGE_DEP_HITS=$((STAGE_DEP_HITS + 1))
    fi
  done
  if [[ "${STAGE_DEP_HITS}" -lt "${STRICT_ASSERT_MIN_DEPLOY_HITS}" ]]; then
    echo "FAIL: trace_id ${TRACE} does not appear in >=${STRICT_ASSERT_MIN_DEPLOY_HITS} worker deployments (hits=${STAGE_DEP_HITS})" >&2
    exit 2
  fi
  if ! echo "${WR_LINES}" | grep -Eq "event=omni_actions_in|event=action_emitted|event=action_feedback_published|REQUIRES_HUMAN"; then
    if [[ "${STRICT_ASSERT_INCLUDE_ADVISORY_MARKERS:-0}" == "1" ]] && echo "${WR_LINES}" | grep -Eq \
      "advisory_analyst_ok|audit_block_written|ADVISORY_DECISION|event=advisory_telegram_sent|telegram_outbound_ok|phase=advisory_render|SUGGEST_REMEDIATION"; then
      true
    else
    echo "FAIL: trace_id ${TRACE} has no action/feedback/terminal markers" >&2
    exit 3
    fi
  fi
  echo "PASS: strict stage assertions satisfied (worker_deploy_hits=${STAGE_DEP_HITS}, min=${STRICT_ASSERT_MIN_DEPLOY_HITS})"
fi

# Optional: assert diagnostic policy / reasoning_chain markers (nginx waiting fault lab).
if [[ "${E2E_ASSERT_DIAGNOSTIC_POLICY:-}" == "1" ]]; then
  echo ""
  echo "=== 3c) Diagnostic policy markers (optional) ==="
  if [[ -z "${WR_LINES:-}" ]]; then
    echo "SKIP: no worker log lines for trace (set WR_LINES from step 3)" >&2
  elif ! echo "${WR_LINES}" | grep -Eq "DIAGNOSTIC_INVARIANT_GATE|reasoning_chain|INV_NO_RESTART_ON_BROKEN_SPEC|PLANNER_READONLY_ROUTE|ERR_SEM_CHANNEL_MISMATCH|diagnostic_invariant_gate|agentic_mutate_plan|readonly_discovery_redirect|k8s_args_coerced"; then
    echo "FAIL: trace_id ${TRACE} has no diagnostic policy / planner-route / agentic-discovery markers in worker logs" >&2
    exit 4
  else
    echo "PASS: diagnostic policy markers present"
  fi
fi

echo ""
echo "=== 4) Loki query_range (Promtail: namespace + pod_name) ==="
LOKI_POD_RE='omni-prober.*|omni-analyst.*|omni-core.*|omni-executor.*|omni-gateway.*|omni-worker.*'
E2E_LOKI_LIMIT="${E2E_LOKI_LIMIT:-500}"
echo "LogQL (Grafana Explore): {namespace=\"${NS}\", pod_name=~\"${LOKI_POD_RE}\"} |= \"$TRACE\""
echo "limit=${E2E_LOKI_LIMIT}"
"${KUBE}" exec -i -n "$NS" "deploy/${EXEC_DEPLOY}" -- env \
  TRACE="${TRACE}" LOKI_URL="${LOKI_URL}" E2E_LOKI_LIMIT="${E2E_LOKI_LIMIT}" E2E_LOKI_NS="${NS}" \
  python3 - <<'PYLOKI'
import json
import os
import time
import urllib.parse
import urllib.request

trace = os.environ["TRACE"]
loki = os.environ["LOKI_URL"]
lim = os.environ.get("E2E_LOKI_LIMIT", "500")
loki_ns = os.environ.get("E2E_LOKI_NS", "")
LOKI_POD_RE = (
    "omni-prober.*|omni-analyst.*|omni-core.*|omni-executor.*|"
    "omni-gateway.*|omni-worker.*"
)
q = '{namespace="' + loki_ns + '", pod_name=~"' + LOKI_POD_RE + '"} |= "' + trace + '"'
now = int(time.time())
start = (now - 3600) * 10**9
end = now * 10**9
params = urllib.parse.urlencode(
    {"query": q, "limit": lim, "start": str(start), "end": str(end)}
)
url = loki.rstrip("/") + "/loki/api/v1/query_range?" + params
try:
    r = urllib.request.urlopen(url, timeout=35)
    d = json.loads(r.read().decode())
except Exception as e:
    print(json.dumps({"loki_error": str(e)}))
    raise SystemExit(0)

res = d.get("data", {}).get("result") or []
rows = []
by_pod: dict[str, int] = {}
for s in res:
    stream = s.get("stream") or {}
    pod = stream.get("pod_name") or stream.get("pod") or "?"
    for v in s.get("values") or []:
        if len(v) >= 2:
            ts_ns = int(v[0])
            line = v[1]
            rows.append((ts_ns, pod, line))
            by_pod[pod] = by_pod.get(pod, 0) + 1

rows.sort(key=lambda x: x[0])
lines_only = [r[2] for r in rows]

print("--- Loki (last 35 lines, mọi pod) ---")
for ln in lines_only[-35:]:
    print((ln[:600] + "…") if len(ln) > 600 else ln)
if not lines_only:
    print("(empty — kiểm tra Promtail ship namespace " + loki_ns + " / Loki DNS)")

print("")
print("=== 5) Phân tích luồng dữ liệu (Loki, theo trace) ===")
if not rows:
    print("(không có dòng Loki — bỏ qua phân tích)")
    raise SystemExit(0)

print(f"• Tổng dòng index được: {len(rows)} (streams={len(res)})")
print("• Số dòng theo pod_name (Promtail → Loki):")
for pod, n in sorted(by_pod.items(), key=lambda x: -x[1]):
    short = pod[:56] + "…" if len(pod) > 58 else pod
    print(f"    - {short}: {n}")

t0, t1 = rows[0][0], rows[-1][0]
print(f"• Khung thời gian log: {(t1 - t0) / 1e6:.1f} ms (đầu → cuối trong sample)")


def _summarize(line: str) -> tuple[str, str]:
    try:
        j = json.loads(line)
        lg = str(j.get("logger") or "")
        msg = str(j.get("message") or "")
        tid = str(j.get("trace_id") or "")
        return lg, (msg[:140] + "…") if len(msg) > 140 else msg, tid
    except Exception:
        return "", (line[:140] + "…") if len(line) > 140 else line, ""


def _stage_hint(lg: str, msg: str) -> str:
    m = (lg + " " + msg).lower()
    if "start_request" in m:
        return "ingress consumer (Kafka omni-alerts)"
    if "diagnostic_dispatcher" in m or "diagnostic_evidence" in m:
        return "SDK probes → Kafka omni-diagnostic-evidence"
    if "evidence_consumer" in m or "kafka_evidence" in m:
        return "analyst: evidence batch / RAG / actions"
    if "omni_actions_in" in m or "kafka_actions" in m:
        return "executor: omni-actions"
    if "omni-gateway" in m or "webhook" in m:
        return "gateway"
    if "end_request" in m:
        return "consumer kết thúc request"
    return "khác"


print("• Timeline (rút gọn, theo thứ tự thời gian):")
for ts_ns, pod, line in rows[: min(80, len(rows))]:
    lg, sm, _tid = _summarize(line)
    stage = _stage_hint(lg, sm)
    pshort = pod.replace("omni-", "")[:24]
    print(f"    [{ts_ns}] {pshort:26} | {stage}")
    if lg:
        print(f"               {lg}: {sm}")

# Luồng nghiệp vụ (một đoạn)
print("• Diễn giải luồng (data path):")
has = " ".join(lines_only).lower()
steps = []
if "start_request" in has or "alert_kafka_in" in has:
    steps.append("Kafka omni-alerts → omni-prober stream_consumer nhận envelope Prometheus/Alertmanager.")
if "diagnostic_dispatcher" in has:
    steps.append("Prober lập kế hoạch probe (SDK + Prom) và publish evidence lên omni-diagnostic-evidence.")
if "evidence_consumer" in has or "diag_batch" in has:
    steps.append("omni-analyst evidence_consumer: so alert vs SDK (STATE_MACHINE_CONTRAST) nếu đủ evidence, else RAG / LLM.")
if "omni_actions_in" in has or "kafka_actions_consumer" in has:
    steps.append("omni-executor consume omni-actions (audit / suggest) cùng trace_id.")
if "end_request" in has:
    steps.append("Prober end_request — vòng alert đóng trong consumer.")
if not steps:
    steps.append("(Không khớp pattern chuẩn — xem raw log phía trên.)")
for i, s in enumerate(steps, 1):
    print(f"    {i}. {s}")
PYLOKI

echo ""
echo "=== 6) Checklist nghiệp vụ ==="
echo "• Default alert: pod nginx-test (E2E_NGINX_POD_AUTO patch từ app=nginx-test); alert labels.namespace = ${NS}."
echo "• MPV3 split: trace xuất hiện trước hết ở omni-prober (omni-alerts); analyst = evidence loop."
echo "• omni-executor: expect event=omni_actions_in action=SUGGEST_REMEDIATION (English diagnosis) — not legacy ping."
echo "• Dùng trace trong Grafana Explore Loki:  $TRACE"

if [[ "${E2E_ASSERT_TELEGRAM_BOT_API:-}" == "1" ]]; then
  echo ""
  echo "=== 7) Telegram Bot API assert (getUpdates) ==="
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    echo "FAIL: E2E_ASSERT_TELEGRAM_BOT_API=1 but TELEGRAM_BOT_TOKEN unset" >&2
    exit 7
  fi
  export E2E_TELEGRAM_POLL_SEC="${E2E_TELEGRAM_POLL_SEC:-300}"
  if ! python3 "${ROOT}/scripts/e2e_telegram_bot_api_assert.py" "$TRACE"; then
    echo "FAIL: Telegram Bot API assert did not find advisory for trace_id=$TRACE" >&2
    exit 8
  fi
  echo "PASS: Telegram Bot API assert (getUpdates text or deleteMessage delivery proof)"
fi
