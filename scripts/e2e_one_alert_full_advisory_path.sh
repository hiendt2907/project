#!/usr/bin/env bash
# Full E2E — một alert qua gateway → Kafka → prober → evidence → analyst → LLM advisory + CRAT + Telegram.
# Không dùng alert CPU HighCPUUsage (thường thoát sớm STATE_MACHINE_CONTRAST, không qua advisory analyst đầy đủ).
#
# Mặc định: alertmanager_nginx_waiting_fault.json (symptom pod_container / broken spec — tới RAG/LLM).
# Env: TELEGRAM_BOT_TOKEN + OMNI_TELEGRAM_ADMIN_CHAT_ID (hoặc đọc Secret cluster như gateway_alert_loki_verify).
#
# Usage:
#   export NS=multi-agent  # hoặc namespace đích (bắt buộc)
#   export E2E_ASSERT_TELEGRAM_BOT_API=1   # assert Bot API (deleteMessage fallback)
#   bash scripts/e2e_one_alert_full_advisory_path.sh
#   bash scripts/e2e_one_alert_full_advisory_path.sh /path/to/alert.json
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PAYLOAD="${1:-${ROOT}/scripts/alert_payloads/alertmanager_nginx_waiting_fault.json}"
KUBE="${ROOT}/scripts/with_working_kube.sh"
if [[ -z "${NS:-}" ]]; then
  echo "e2e_one_alert_full_advisory_path.sh: set NS to the Kubernetes namespace (no default)." >&2
  exit 2
fi

# Đủ thời gian Ollama + vòng agentic (waiting fault / planner).
export SLEEP_SEC="${SLEEP_SEC:-120}"
export E2E_EXTRA_AGENTIC_SLEEP="${E2E_EXTRA_AGENTIC_SLEEP:-300}"
export E2E_TELEGRAM_POLL_SEC="${E2E_TELEGRAM_POLL_SEC:-600}"
export STRICT_ASSERT="${STRICT_ASSERT:-1}"
# Advisory path often chỉ thấy trace rõ trên prober + analyst (core/executor không log cùng trace).
export STRICT_ASSERT_MIN_DEPLOY_HITS="${STRICT_ASSERT_MIN_DEPLOY_HITS:-2}"
# Cho phép PASS strict 3b khi chỉ có advisory/CRAT/Telegram suggest (không qua omni-executor).
export STRICT_ASSERT_INCLUDE_ADVISORY_MARKERS="${STRICT_ASSERT_INCLUDE_ADVISORY_MARKERS:-1}"
export E2E_ASSERT_TELEGRAM_BOT_API="${E2E_ASSERT_TELEGRAM_BOT_API:-1}"
# Telegram assert: tắt delete nếu cần giữ tin trên chat
# export E2E_TELEGRAM_VERIFY_DELETE_MESSAGE=0

if [[ ! -f "$PAYLOAD" ]]; then
  echo "FAIL: payload not found: $PAYLOAD" >&2
  exit 1
fi

echo "=== e2e_one_alert_full_advisory_path: payload=$(basename "$PAYLOAD") SLEEP_SEC=$SLEEP_SEC E2E_EXTRA_AGENTIC_SLEEP=$E2E_EXTRA_AGENTIC_SLEEP ==="

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  if "${KUBE}" get secret telegram-bot -n "$NS" &>/dev/null; then
    export TELEGRAM_BOT_TOKEN="$("${KUBE}" get secret telegram-bot -n "$NS" -o jsonpath='{.data.bot-token}' | base64 -d)"
    export OMNI_TELEGRAM_ADMIN_CHAT_ID="${OMNI_TELEGRAM_ADMIN_CHAT_ID:-$("${KUBE}" get secret telegram-bot -n "$NS" -o jsonpath='{.data.chat-id}' | base64 -d | tr -d '\n\r ')}"
    echo "=== Loaded TELEGRAM_BOT_TOKEN / OMNI_TELEGRAM_ADMIN_CHAT_ID from secret telegram-bot ==="
  fi
fi

LOGF="$(mktemp "${TMPDIR:-/tmp}/e2e-full-adv.XXXXXX.log")"
trap 'rm -f "${LOGF}"' EXIT

set +o pipefail
bash "${ROOT}/scripts/gateway_alert_loki_verify.sh" "$PAYLOAD" 2>&1 | tee "${LOGF}"
rc="${PIPESTATUS[0]}"
set -o pipefail
if [[ "${rc}" -ne 0 ]]; then
  exit "${rc}"
fi

TRACE="$(grep -E '^trace_id=' "${LOGF}" 2>/dev/null | tail -1 | cut -d= -f2 || true)"
if [[ -n "${TRACE}" ]] && [[ "${E2E_ASSERT_FULL_ADVISORY_LLM:-1}" == "1" ]]; then
  echo ""
  echo "=== E2E_ASSERT_FULL_ADVISORY_LLM: expect LLM advisory path for ${TRACE} ==="
  AL="$("${KUBE}" logs -n "$NS" deploy/omni-analyst --since=40m --tail=25000 2>/dev/null || true)"
  if echo "${AL}" | grep -F "${TRACE}" | grep -Eq 'source=advisory_render|advisory_telegram_sent|advisory_analyst_ok|event=advisory_analyst|phase=advisory_analyst|audit_block_written|ADVISORY_DECISION'; then
    echo "PASS: full LLM advisory / CRAT markers for trace"
  else
    echo "FAIL: no LLM advisory markers for ${TRACE} (pipeline may have used STATE_MACHINE_CONTRAST or invariant only)" >&2
    echo "--- analyst lines (trace tail) ---" >&2
    echo "${AL}" | grep -F "${TRACE}" | tail -40 >&2 || true
    exit 9
  fi
fi

echo ""
echo "=== Done: trace=${TRACE:-unknown} ==="
