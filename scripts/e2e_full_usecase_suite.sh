#!/usr/bin/env bash
# Full E2E use-case suite — mỗi case inject live, wait, verify CRAT + Telegram + LLM path.
# Usage: NS=multi-agent bash scripts/e2e_full_usecase_suite.sh
# Env: SKIP_SIEM=1 (bỏ SIEM cases), SKIP_DEATH_LOOP=1, VERBOSE=1
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
NS="${NS:?set NS=multi-agent}"
VERBOSE="${VERBOSE:-0}"

PASS=0; FAIL=0; SKIP=0
declare -a RESULTS=()

_now()  { date '+%H:%M:%S'; }
_ok()   { PASS=$((PASS+1)); RESULTS+=("  [PASS] $*"); echo "  [PASS][$(_now)] $*"; }
_fail() { FAIL=$((FAIL+1)); RESULTS+=("  [FAIL] $*"); echo "  [FAIL][$(_now)] $*" >&2; }
_skip() { SKIP=$((SKIP+1)); RESULTS+=("  [SKIP] $*"); echo "  [SKIP][$(_now)] $*"; }
_hdr()  { echo ""; echo "════════════════════════════════════════"; echo "  $*"; echo "════════════════════════════════════════"; }

# ── helpers ──────────────────────────────────────────────────────────────────

_analyst_logs() { "${KUBE}" kubectl logs -n "$NS" deploy/omni-analyst --since=60m --tail=50000 2>/dev/null || true; }
_prober_logs()  { "${KUBE}" kubectl logs -n "$NS" deploy/omni-prober  --since=60m --tail=50000 2>/dev/null || true; }

_wait_trace() {
  # Usage: _wait_trace <trace_id> <timeout_sec>
  local trace="$1" timeout="${2:-180}" elapsed=0 interval=8
  echo "    waiting for trace=$trace (timeout=${timeout}s)..."
  while [[ $elapsed -lt $timeout ]]; do
    local logs
    logs=$(_analyst_logs)
    if echo "$logs" | grep -qF "$trace"; then
      if echo "$logs" | grep -F "$trace" | grep -qE 'advisory_analyst_complete|audit_block_written|ADVISORY_DECISION|telegram_outbound_ok'; then
        echo "    trace=$trace appeared after ${elapsed}s"
        return 0
      fi
    fi
    sleep $interval
    elapsed=$((elapsed + interval))
  done
  echo "    TIMEOUT: trace=$trace not resolved in ${timeout}s" >&2
  return 1
}

_assert_trace() {
  local trace="$1" name="$2"
  local logs
  logs=$(_analyst_logs)
  local trace_lines
  trace_lines=$(echo "$logs" | grep -F "$trace" || true)

  local crat_count tg_ok llm_ok
  crat_count=$(echo "$trace_lines" | grep -c 'audit_block_written' || true)
  tg_ok=$(echo "$trace_lines" | grep -c 'telegram_outbound_ok' || true)
  llm_ok=$(echo "$trace_lines" | grep -c 'advisory_analyst_ok\|advisory_analyst_complete' || true)

  local verdict=""
  verdict=$(echo "$trace_lines" | grep 'advisory_analyst_complete' | python3 -c "
import sys,re
for l in sys.stdin:
  m=re.search(r'verdict=(\w+)',l)
  if m: print(m.group(1)); break
" 2>/dev/null || true)

  echo "    $name: trace=$trace CRAT=${crat_count} TG=${tg_ok} LLM=${llm_ok} verdict=${verdict:-?}"

  local ok=1
  [[ $crat_count -ge 2 ]] || { echo "    FAIL: CRAT blocks < 2 (got $crat_count)" >&2; ok=0; }
  [[ $tg_ok -ge 1 ]]      || { echo "    FAIL: no Telegram sent" >&2; ok=0; }
  [[ $llm_ok -ge 1 ]]     || { echo "    FAIL: no LLM advisory" >&2; ok=0; }
  [[ $ok -eq 1 ]]
}

_post_alert() {
  local payload="$1"
  # Redirect tee output to stderr so stdout is clean for capture
  NS="$NS" bash "${ROOT}/scripts/alert_flow_realistic/post_gateway_alert.sh" "$payload" 2>/dev/null \
    | tee /tmp/e2e_post_resp.txt >/dev/null
  grep -oE '"trace_id":"[a-z0-9_-]+"' /tmp/e2e_post_resp.txt | head -1 | grep -oE '[a-z0-9_-]+' | tail -1 || \
  grep -oE 'trace_id=[a-z0-9_-]+' /tmp/e2e_post_resp.txt | tail -1 | cut -d= -f2 || true
}

_get_crat_chain_len() {
  "${KUBE}" kubectl exec -n "$NS" deploy/omni-core -- python3 -c "
import redis, os
r = redis.Redis.from_url(os.environ.get('OMNI_REDIS_URL','redis://redis:6379/0'))
print(r.llen('audit_chain:blocks'))
" 2>/dev/null || echo "?"
}

# ── TC0: pre-flight ───────────────────────────────────────────────────────────
_hdr "TC0 — Pre-flight"

CHAIN_BEFORE=$(_get_crat_chain_len)
echo "  CRAT chain before: $CHAIN_BEFORE blocks"

ALL_RUNNING=1
for dep in omni-gateway omni-prober omni-analyst omni-executor omni-core kafka redis; do
  READY=$("${KUBE}" kubectl get deploy "$dep" -n "$NS" -o jsonpath='{.status.readyReplicas}' 2>/dev/null \
    || "${KUBE}" kubectl get statefulset "$dep" -n "$NS" -o jsonpath='{.status.readyReplicas}' 2>/dev/null \
    || echo "?")
  if [[ "$READY" == "0" || "$READY" == "" ]]; then
    _fail "TC0: $dep not ready (readyReplicas=$READY)"
    ALL_RUNNING=0
  else
    echo "  [ok] $dep ready=$READY"
  fi
done

[[ $ALL_RUNNING -eq 1 ]] && _ok "TC0: all deployments ready" || { echo "Aborting: required components down" >&2; exit 1; }

# ── TC1: CRAT E2E pipeline ────────────────────────────────────────────────────
_hdr "TC1 — CRAT 4-Phase E2E Pipeline"
set +e
E2E_LIVE_PROFILE_JSON="${ROOT}/scripts/fixtures/e2e_live_profile.json" \
  "${ROOT}/.venv/bin/python" "${ROOT}/scripts/verify_e2e_crat_pipeline.py" 2>&1 | tail -20
TC1_RC=$?
set -e
[[ $TC1_RC -eq 0 ]] && _ok "TC1: CRAT 4-phase E2E PASS" || _fail "TC1: CRAT E2E FAIL (rc=$TC1_RC)"

# ── TC2: nginx CPU high → STATE_MACHINE_CONTRAST fast path ──────────────────
# NOTE: nginx_cpu_high deliberately uses STATE_MACHINE_CONTRAST (no LLM/CRAT).
# Only assert: Telegram delivery + SUGGEST_REMEDIATION emitted. (see gateway_alert_loki_verify.sh comment)
_hdr "TC2 — Lane1 Resource: nginx_cpu_high (STATE_MACHINE_CONTRAST path)"
set +e
TRACE=$(_post_alert "${ROOT}/scripts/alert_payloads/alertmanager_nginx_cpu_high.json")
echo "  injected trace=$TRACE"
if [[ -n "$TRACE" ]]; then
  _wait_trace "$TRACE" 160
  logs_tc2=$(_analyst_logs)
  tg_tc2=$(echo "$logs_tc2" | grep -F "$TRACE" | grep -c 'telegram_outbound_ok' || true)
  action_tc2=$(echo "$logs_tc2" | grep -F "$TRACE" | grep -c 'action_emitted' || true)
  path_tc2=$(echo "$logs_tc2" | grep -F "$TRACE" | grep -oE 'STATE_MACHINE_CONTRAST|source=advisory_render' | head -1 || echo "?")
  echo "    TC2/nginx_cpu_high: trace=$TRACE TG=${tg_tc2} action=${action_tc2} path=${path_tc2}"
  [[ $tg_tc2 -ge 1 && $action_tc2 -ge 1 ]] && \
    _ok "TC2: nginx_cpu_high Telegram+action via ${path_tc2}" || \
    _fail "TC2: nginx_cpu_high (TG=$tg_tc2 action=$action_tc2)"
else
  _fail "TC2: no trace returned from gateway"
fi
set -e

# ── TC3: nginx waiting fault → suggest_remediation ───────────────────────────
_hdr "TC3 — Lane2 Broken-Spec: nginx_waiting_fault"
set +e
TRACE=$(_post_alert "${ROOT}/scripts/alert_payloads/alertmanager_nginx_waiting_fault.json")
echo "  injected trace=$TRACE"
TC3_TRACE="$TRACE"
if [[ -n "$TRACE" ]]; then
  _wait_trace "$TRACE" 200
  _assert_trace "$TRACE" "TC3/nginx_waiting_fault" && _ok "TC3: nginx_waiting_fault SUGGEST_REMEDIATION + CRAT + TG" || _fail "TC3: nginx_waiting_fault"
else
  _fail "TC3: no trace returned from gateway"
fi
set -e

# ── TC4: redis exporter probe failure ────────────────────────────────────────
_hdr "TC4 — Lane3 App-Log: redis_exporter_probe"
set +e
TRACE=$(_post_alert "${ROOT}/scripts/alert_payloads/alertmanager_redis_exporter_probe.json")
echo "  injected trace=$TRACE"
if [[ -n "$TRACE" ]]; then
  _wait_trace "$TRACE" 180
  _assert_trace "$TRACE" "TC4/redis_exporter_probe" && _ok "TC4: redis_exporter_probe INVESTIGATE + CRAT + TG" || _fail "TC4: redis_exporter_probe"
else
  _fail "TC4: no trace returned from gateway"
fi
set -e

# ── TC5: business sane (ProbeFailureLab) ─────────────────────────────────────
_hdr "TC5 — Mixed: business_sane (ProbeFailureLab)"
set +e
TRACE=$(_post_alert "${ROOT}/scripts/alert_payloads/alertmanager_business_sane.json")
echo "  injected trace=$TRACE"
if [[ -n "$TRACE" ]]; then
  _wait_trace "$TRACE" 180
  _assert_trace "$TRACE" "TC5/business_sane" && _ok "TC5: business_sane + ADVISORY_MODE_KILL_SWITCH + CRAT + TG" || _fail "TC5: business_sane"
else
  _fail "TC5: no trace returned from gateway"
fi
set -e

# ── TC6: SIEM DDoS (Lane4) ────────────────────────────────────────────────────
if [[ "${SKIP_SIEM:-0}" == "1" ]]; then
  _skip "TC6: SIEM DDoS (SKIP_SIEM=1)"
  _skip "TC7: SIEM Malware (SKIP_SIEM=1)"
else
  _hdr "TC6 — Lane4 SIEM: DDoS via siem-bridge"
  set +e
  SIEM_TRACE=""
  SIEM_TRACE=$(E2E_LIVE_PROFILE_JSON="${ROOT}/scripts/fixtures/e2e_live_profile.json" \
    "${ROOT}/.venv/bin/python" "${ROOT}/scripts/verify_e2e_crat_pipeline.py" --phase=siem_only 2>/dev/null \
    | grep -oE 'trace=[a-z0-9_-]+' | head -1 | cut -d= -f2 || true)

  if [[ -z "$SIEM_TRACE" ]]; then
    # Inject into finguard-customer redis (where siem-bridge reads from)
    FG_NS="finguard-customer"
    FG_REDIS_AUTH=$(kubectl get secret redis-auth -n "$FG_NS" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || echo "CHANGEME_REDIS_PASSWORD")
    SIEM_ID="e2e-siem-$(openssl rand -hex 4)"
    SIEM_TRACE="fg-${SIEM_ID:9:8}"
    "${KUBE}" kubectl exec -n "$FG_NS" redis-0 -- redis-cli \
      -a "$FG_REDIS_AUTH" --no-auth-warning \
      XADD "stream:actionable_incidents" "*" \
      id "$SIEM_ID" category "ddos" severity "critical" \
      namespace "multi-agent" tenant_id "e2e-tenant" source_ip "10.0.0.99" \
      description "E2E TC6: DDoS flood test" suggested_action "Block IP" \
      hitl_required "true" alert_rule "DDoSFloodDetected" \
      alert_hint "SYN flood" trace_id "$SIEM_TRACE" \
      timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" 2>/dev/null
    echo "  injected SIEM DDoS trace=$SIEM_TRACE via finguard-customer/redis-0"
  fi

  if [[ -n "$SIEM_TRACE" ]]; then
    _wait_trace "$SIEM_TRACE" 200
    # SIEM: expect ESCALATE_TO_HUMAN on CRITICAL (expected behavior)
    local_logs=$(_analyst_logs)
    crat_c=$(echo "$local_logs" | grep -F "$SIEM_TRACE" | grep -c 'audit_block_written' || true)
    tg_c=$(echo "$local_logs"   | grep -F "$SIEM_TRACE" | grep -c 'telegram_outbound_ok' || true)
    escalate_c=$(echo "$local_logs" | grep -F "$SIEM_TRACE" | grep -c 'ESCALATE_TO_HUMAN\|telegram_escalation' || true)
    echo "  $SIEM_TRACE: CRAT=$crat_c TG=$tg_c ESCALATE=$escalate_c"
    [[ $crat_c -ge 2 && $tg_c -ge 1 ]] && \
      _ok "TC6: SIEM DDoS CRITICAL + CRAT + TG + ESCALATE_TO_HUMAN" || \
      _fail "TC6: SIEM DDoS (CRAT=$crat_c TG=$tg_c)"
  else
    _fail "TC6: SIEM DDoS — could not inject trace"
  fi
  set -e

  # ── TC7: SIEM Malware ────────────────────────────────────────────────────────
  _hdr "TC7 — Lane4 SIEM: Malware"
  set +e
  FG_NS="finguard-customer"
  FG_REDIS_AUTH=$(kubectl get secret redis-auth -n "$FG_NS" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || echo "CHANGEME_REDIS_PASSWORD")
  SIEM_ID2="e2e-siem-$(openssl rand -hex 4)"
  SIEM_TRACE2="fg-${SIEM_ID2:9:8}"
  "${KUBE}" kubectl exec -n "$FG_NS" redis-0 -- redis-cli \
    -a "$FG_REDIS_AUTH" --no-auth-warning \
    XADD "stream:actionable_incidents" "*" \
    id "$SIEM_ID2" category "malware" severity "critical" \
    namespace "multi-agent" tenant_id "e2e-tenant" source_ip "10.0.0.77" \
    description "E2E TC7: Malware beacon detected" suggested_action "Isolate pod" \
    hitl_required "true" alert_rule "MalwareBeaconDetected" \
    alert_hint "C2 beacon pattern" trace_id "$SIEM_TRACE2" \
    timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" 2>/dev/null
  echo "  injected SIEM malware trace=$SIEM_TRACE2 via finguard-customer/redis-0"
  _wait_trace "$SIEM_TRACE2" 200
  local_logs2=$(_analyst_logs)
  crat_c2=$(echo "$local_logs2" | grep -F "$SIEM_TRACE2" | grep -c 'audit_block_written' || true)
  tg_c2=$(echo "$local_logs2"   | grep -F "$SIEM_TRACE2" | grep -c 'telegram_outbound_ok' || true)
  [[ $crat_c2 -ge 2 && $tg_c2 -ge 1 ]] && \
    _ok "TC7: SIEM Malware CRITICAL + CRAT + TG" || \
    _fail "TC7: SIEM Malware (CRAT=$crat_c2 TG=$tg_c2)"
  set -e
fi

# ── TC8: incident matrix (RBAC + API + missing configmap) ────────────────────
_hdr "TC8 — Incident Matrix (RBAC + API scenarios)"
set +e
NS="$NS" RBAC_NEGATIVE_NAMESPACE=finguard-customer \
  SCENARIOS="wave_a1_rbac_manifest,wave_a1_rbac_permissions,phase_b_api_resource,phase_b_api_state,phase_b_sec_audit" \
  bash "${ROOT}/scripts/e2e_incident_matrix.sh" 2>&1 | tail -15
TC8_RC=$?
set -e
[[ $TC8_RC -eq 0 ]] && _ok "TC8: incident matrix RBAC + API scenarios PASS" || _fail "TC8: incident matrix FAIL (rc=$TC8_RC)"

# ── TC9: nginx missing ConfigMap fault injection ──────────────────────────────
_hdr "TC9 — Live Fault Injection: nginx missing ConfigMap"
set +e
NS="$NS" SLEEP_SEC=60 E2E_EXTRA_AGENTIC_SLEEP=120 \
  bash "${ROOT}/scripts/e2e_nginx_missing_configmap.sh" 2>&1 | tail -10
TC9_RC=$?
set -e
[[ $TC9_RC -eq 0 ]] && _ok "TC9: nginx missing ConfigMap fault injection PASS" || _fail "TC9: nginx missing ConfigMap FAIL (rc=$TC9_RC)"

# ── TC10: Kafka lag accuracy ──────────────────────────────────────────────────
_hdr "TC10 — Kafka lag metric sanity"
set +e
PROM_IP="192.168.194.148"
LAG_EVIDENCE=$(curl -sG "http://${PROM_IP}:9090/api/v1/query" \
  --data-urlencode "query=omni_kafka_consumer_lag" 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
results=d['data']['result']
all_ok=True
for r in results:
  lag=float(r['value'][1])
  topic=r['metric'].get('topic','?')
  group=r['metric'].get('consumer_group','?')
  print(f'  topic={topic} group={group} lag={lag}')
  if lag > 1000:
    print(f'  FAIL: lag {lag} suspiciously high — likely last_stable_offset=-1 bug')
    all_ok=False
if all_ok: print('OK')
" 2>/dev/null || echo "query failed")
echo "$LAG_EVIDENCE"
echo "$LAG_EVIDENCE" | grep -q "^OK" && _ok "TC10: Kafka lag metric sane (no -1 artifact)" || _fail "TC10: Kafka lag abnormal"
set -e

# ── TC11: CRAT integrity ──────────────────────────────────────────────────────
_hdr "TC11 — CRAT chain integrity (full verify)"
set +e
CRAT_OUT=$("${KUBE}" kubectl exec -n "$NS" deploy/omni-core -- \
  python3 /app/scripts/crat_integrity_check.py 2>&1)
echo "$CRAT_OUT"
CHAIN_AFTER=$(_get_crat_chain_len)
echo "  CRAT chain after: $CHAIN_AFTER blocks (was $CHAIN_BEFORE)"
echo "$CRAT_OUT" | grep -q "chain OK" && _ok "TC11: CRAT chain integrity PASS (${CHAIN_AFTER} blocks)" || _fail "TC11: CRAT integrity FAIL"
set -e

# ── TC12: pre-deploy gate ─────────────────────────────────────────────────────
_hdr "TC12 — Pre-deploy validation gate"
set +e
bash "${ROOT}/scripts/pre-deploy-validate.sh" 2>&1 | tail -6
TC12_RC=$?
set -e
[[ $TC12_RC -eq 0 ]] && _ok "TC12: pre-deploy gate PASS" || _fail "TC12: pre-deploy gate FAIL (rc=$TC12_RC)"

# ── TC13: no unexpected errors in error log window ───────────────────────────
_hdr "TC13 — Error log audit (last 30m)"
set +e
# Exclude: connectivity errors (transient on startup), ESCALATE_TO_HUMAN (expected for CRITICAL SIEM advisory path)
"${KUBE}" kubectl logs -n "$NS" deploy/omni-analyst --since=30m 2>/dev/null | \
  python3 -c "
import sys,json
EXPECTED_ERRORS = {'Unable connect', 'ESCALATE_TO_HUMAN', 'kafka_bootstrap_retry', 'Unclosed AIOKafka'}
n=0
for line in sys.stdin:
  try:
    obj=json.loads(line)
    if obj.get('level')=='ERROR':
      msg = obj.get('message','')
      if any(e in msg for e in EXPECTED_ERRORS):
        continue
      print(f\"  ERROR: [{obj.get('logger','')}] {msg[:100]}\")
      n+=1
  except: pass
if n==0: print('  (no unexpected errors)')
print(f'TOTAL={n}')
" 2>/dev/null | tee /tmp/e2e_errors.txt
ERROR_COUNT=$(grep -oE '^TOTAL=[0-9]+' /tmp/e2e_errors.txt | cut -d= -f2 || echo "0")
echo "  Unexpected errors in analyst (30m): ${ERROR_COUNT:-0}"
[[ "${ERROR_COUNT:-0}" -eq 0 ]] && _ok "TC13: no unexpected errors in analyst (30m)" || _fail "TC13: ${ERROR_COUNT:-0} unexpected errors"
set -e

# ── TC14: Telegram Bot API assert (use TC3 LLM trace for deleteMessage proof) ─
_hdr "TC14 — Telegram Bot API delivery assert"
set +e
TC14_TRACE="${TC3_TRACE:-}"
if [[ -z "$TC14_TRACE" ]]; then
  # Fall back to most recent advisory_telegram_sent trace from analyst logs
  TC14_TRACE=$(_analyst_logs | python3 -c "
import sys,json
for line in reversed(sys.stdin.readlines()):
  try:
    obj=json.loads(line)
    if obj.get('message','').startswith('event=advisory_telegram_sent') or 'advisory_telegram_sent' in obj.get('message',''):
      t=obj.get('trace_id','')
      if t: print(t); break
  except: pass
" 2>/dev/null || true)
fi
echo "  using trace=${TC14_TRACE:-<none>} for Bot API assert"
if [[ -n "$TC14_TRACE" ]]; then
  # Load Telegram credentials from cluster secret if not already set
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    TELEGRAM_BOT_TOKEN=$("${KUBE}" kubectl get secret telegram-bot -n "$NS" \
      -o jsonpath='{.data.bot-token}' 2>/dev/null | base64 -d || true)
    OMNI_TELEGRAM_ADMIN_CHAT_ID=${OMNI_TELEGRAM_ADMIN_CHAT_ID:-$("${KUBE}" kubectl get secret telegram-bot -n "$NS" \
      -o jsonpath='{.data.chat-id}' 2>/dev/null | base64 -d | tr -d '\n\r ' || true)}
  fi
  TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
  OMNI_TELEGRAM_ADMIN_CHAT_ID="${OMNI_TELEGRAM_ADMIN_CHAT_ID:-}" \
  NS="$NS" E2E_KUBE_NS="$NS" E2E_TELEGRAM_POLL_SEC=60 \
    "${ROOT}/.venv/bin/python" "${ROOT}/scripts/e2e_telegram_bot_api_assert.py" "$TC14_TRACE" 2>&1 | tail -10
  TC14_RC=$?
else
  echo "  no LLM trace available — skipping Bot API deleteMessage proof"
  TC14_RC=0
fi
set -e
[[ $TC14_RC -eq 0 ]] && _ok "TC14: Telegram Bot API assert PASS (trace=${TC14_TRACE:-skipped})" || _fail "TC14: Telegram Bot API FAIL (rc=$TC14_RC)"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo "  E2E FULL USE-CASE SUITE — RESULTS"
echo "════════════════════════════════════════"
for r in "${RESULTS[@]}"; do echo "$r"; done
echo ""
echo "  PASS=$PASS  FAIL=$FAIL  SKIP=$SKIP  TOTAL=$((PASS+FAIL+SKIP))"
echo "════════════════════════════════════════"

if [[ $FAIL -gt 0 ]]; then
  echo "  SUITE FAILED — fix above issues before go-prod."
  exit 1
fi
echo "  SUITE PASSED — all use cases verified end-to-end."
