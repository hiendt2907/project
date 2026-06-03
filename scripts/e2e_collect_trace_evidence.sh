#!/usr/bin/env bash
# Thu nhanh bằng chứng trust-but-verify + death-loop cho một trace_id (grep counts trên log pod).
#
# Usage:
#   NS=multi-agent bash scripts/e2e_collect_trace_evidence.sh '<trace_id>'
#   bash scripts/e2e_collect_trace_evidence.sh gw-prom-abc123
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
NS="${NS:-multi-agent}"
TRACE="${1:?usage: $0 <trace_id>}"

echo "=== e2e_collect_trace_evidence trace=${TRACE} ns=${NS} ==="
echo ""

_collect() {
  local dep="$1"
  "${KUBE}" logs -n "$NS" "deploy/${dep}" --since=45m --tail=80000 2>/dev/null | grep -F "$TRACE" || true
}

# Split-role pods consolidated into omni-fullstack (2026-06-03) — single log source.
RAW="$(_collect omni-fullstack)
"

_count_sub() {
  echo -n "$RAW" | grep -F "$1" | wc -l | tr -d ' '
}

echo "--- counts (grep -F on pod logs, same trace) ---"
echo "count_command_feedback_ingested=$(_count_sub 'transition=COMMAND_FEEDBACK_INGESTED')"
echo "count_action_feedback_received=$(_count_sub 'action_feedback_received')"
echo "count_action_feedback_published=$(_count_sub 'action_feedback_published')"
echo "count_omni_actions_in=$(_count_sub 'event=omni_actions_in')"
echo "count_plan_emitted=$(_count_sub 'PLAN_EMITTED')"
echo "count_audit_block_written=$(_count_sub 'audit_block_written')"
echo "count_telegram_outbound_ok=$(_count_sub 'telegram_outbound_ok')"
echo ""
echo "--- terminal / cap markers (subset) ---"
echo "$RAW" | grep -E 'STATE_VERIFY_MAX_ATTEMPTS|ESC_MAX_ATTEMPTS|tombstone|action_feedback_success|VERIFIED_SUCCESS|REQUIRES_HUMAN|TRANSITION_STATE_MACHINE_VERIFIED' | tail -n 20 || echo "(none matched)"
echo ""
echo "--- tail trace lines (last 30) ---"
echo "$RAW" | tail -n 30
