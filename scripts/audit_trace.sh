#!/usr/bin/env bash
# audit_trace.sh — verify whether a trace ID ended in confirmed execution or surrender.
#
# Usage:
#   bash scripts/audit_trace.sh <TRACE_ID> [--timeout 120]
#
# Exit codes:
#   0  — mutation confirmed (EXECUTE_MUTATE reached executor + action_feedback_published)
#   1  — SUGGEST_REMEDIATION (planner gave up or was blocked, no execution)
#   2  — inconclusive (neither signal found within timeout; timeout expired)
#   3  — usage error
#
# Detection logic (in order of precedence):
#   SUCCESS  → analyst log: "[DATA] *_ok"  AND executor log: event=action_feedback_published
#   FAILURE  → analyst log: event=omni_actions_audit_only action=SUGGEST_REMEDIATION
#              (found in omni-actions consumer / kafka_actions_consumer)
#   CLOSED   → autonomous_feedback_loop log: event=autonomous_case_closed
#
# Requires: kubectl access to multi-agent namespace.

set -euo pipefail

TRACE_ID="${1:-}"
TIMEOUT_SEC="${3:-120}"   # positional 2 is --timeout flag; 3 is value
if [[ "${2:-}" == "--timeout" ]]; then
    TIMEOUT_SEC="${3:-120}"
fi

if [[ -z "$TRACE_ID" ]]; then
    echo "Usage: $0 <TRACE_ID> [--timeout <seconds>]"
    exit 3
fi

NS="multi-agent"
POLL_INTERVAL=5

echo "=== audit_trace: TRACE_ID=${TRACE_ID} timeout=${TIMEOUT_SEC}s ==="
echo ""

_grep_analyst() {
    kubectl logs -n "$NS" -l "app=omni-analyst" \
        --since="600s" --tail=3000 2>/dev/null | grep -F "$TRACE_ID" || true
}

_grep_executor() {
    kubectl logs -n "$NS" -l "app=omni-executor" \
        --since="600s" --tail=3000 2>/dev/null | grep -F "$TRACE_ID" || true
}

_grep_prober() {
    kubectl logs -n "$NS" -l "app=omni-prober" \
        --since="600s" --tail=3000 2>/dev/null | grep -F "$TRACE_ID" || true
}

_all_logs() {
    { _grep_analyst; _grep_executor; _grep_prober; } 2>/dev/null || true
}

DEADLINE=$(( $(date +%s) + TIMEOUT_SEC ))

while true; do
    NOW=$(date +%s)
    LOGS=$(_all_logs)

    # ── SUCCESS: mutation reached executor and published feedback ──────────────
    FEEDBACK_OK=$(echo "$LOGS" | grep -E "event=action_feedback_published" || true)
    DATA_OK=$(echo "$LOGS" | grep -E "\[DATA\] [a-z_]+_ok" || true)
    if [[ -n "$FEEDBACK_OK" || -n "$DATA_OK" ]]; then
        echo "[PASS] Mutation confirmed for trace=${TRACE_ID}"
        echo ""
        echo "--- execution evidence ---"
        echo "$LOGS" | grep -E "event=action_feedback_published|\[DATA\] [a-z_]+_ok|event=omni_actions_in" | head -20
        echo ""
        # Check for closed-loop case_closed
        CASE_CLOSED=$(echo "$LOGS" | grep "event=autonomous_case_closed" || true)
        if [[ -n "$CASE_CLOSED" ]]; then
            echo "[INFO] Closed-loop verified: event=autonomous_case_closed"
        fi
        exit 0
    fi

    # ── CASE CLOSED without explicit feedback (verify loop completed) ──────────
    CASE_CLOSED=$(echo "$LOGS" | grep "event=autonomous_case_closed" || true)
    if [[ -n "$CASE_CLOSED" ]]; then
        echo "[PASS] Closed-loop case_closed for trace=${TRACE_ID}"
        echo "$CASE_CLOSED" | head -5
        exit 0
    fi

    # ── FAILURE: planner surrendered ──────────────────────────────────────────
    SUGGEST=$(echo "$LOGS" | grep -E "event=omni_actions_audit_only.*action=SUGGEST_REMEDIATION|omni_actions_audit_only.*SUGGEST_REMEDIATION" || true)
    if [[ -n "$SUGGEST" ]]; then
        echo "[FAIL] SUGGEST_REMEDIATION (no execution) for trace=${TRACE_ID}"
        echo ""
        echo "--- surrender evidence ---"
        echo "$SUGGEST" | head -10
        echo ""
        # Print planner's final analysis if present
        DONE_LINE=$(echo "$LOGS" | grep -E "event=planner_phase_done|reason_code=PLANNER_PHASE_DONE" || true)
        if [[ -n "$DONE_LINE" ]]; then
            echo "--- planner phase_done ---"
            echo "$DONE_LINE" | head -5
        fi
        exit 1
    fi

    # ── TIMEOUT ───────────────────────────────────────────────────────────────
    if [[ $NOW -ge $DEADLINE ]]; then
        echo "[INCONCLUSIVE] No decisive signal found for trace=${TRACE_ID} within ${TIMEOUT_SEC}s"
        echo ""
        echo "--- last log lines for this trace ---"
        echo "$LOGS" | tail -20
        exit 2
    fi

    echo "  [wait] ${POLL_INTERVAL}s — no signal yet (elapsed $(( NOW - (DEADLINE - TIMEOUT_SEC) ))s / ${TIMEOUT_SEC}s)"
    sleep $POLL_INTERVAL
done
