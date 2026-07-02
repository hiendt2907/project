#!/usr/bin/env bash
# Append a quota-drain checkpoint entry to the ledger and print a reminder of
# what must be true before the loop is allowed to sleep. This script does
# NOT mutate the state JSON itself (Claude/the skill owns that write) — it
# only appends the human-readable ledger entry and validates the state file.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
LEDGER="${PROJECT_ROOT}/docs/operations/AUTONOMOUS_LOOP_LEDGER.md"
STATE="${PROJECT_ROOT}/docs/operations/AUTONOMOUS_LOOP_STATE.json"

usage() {
  cat <<EOF
Usage: $0 --iteration <id> --acceptance <true|false> --last-verified <text> --pending <text> --reset-at <iso8601|unknown> --resume-action <text>
EOF
  exit 1
}

ITERATION=""; ACCEPTANCE=""; LAST_VERIFIED=""; PENDING=""; RESET_AT=""; RESUME_ACTION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --iteration) ITERATION="$2"; shift 2 ;;
    --acceptance) ACCEPTANCE="$2"; shift 2 ;;
    --last-verified) LAST_VERIFIED="$2"; shift 2 ;;
    --pending) PENDING="$2"; shift 2 ;;
    --reset-at) RESET_AT="$2"; shift 2 ;;
    --resume-action) RESUME_ACTION="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

[[ -n "$ITERATION" && -n "$ACCEPTANCE" ]] || usage

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
HEAD="$(cd "$PROJECT_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo unknown)"

mkdir -p "$(dirname "$LEDGER")"
{
  echo ""
  echo "### Checkpoint ${TS}"
  echo "- Timestamp: ${TS}"
  echo "- Iteration: ${ITERATION}"
  echo "- Quota state: QUOTA_DRAINING"
  echo "- HEAD: ${HEAD}"
  echo "- Acceptance: ${ACCEPTANCE}"
  echo "- Last verified: ${LAST_VERIFIED:-<none>}"
  echo "- Pending: ${PENDING:-<none>}"
  echo "- Reset at: ${RESET_AT:-unknown}"
  echo "- Resume action: ${RESUME_ACTION:-<none>}"
} >> "$LEDGER"

echo "[quota_checkpoint] appended entry to ${LEDGER}"

if [[ -f "$STATE" ]]; then
  python3 "${SCRIPT_DIR}/validate_state.py" --state-file "$STATE" \
    || echo "[quota_checkpoint] WARNING: state file failed validation — fix before sleeping" >&2
else
  echo "[quota_checkpoint] WARNING: state file not found at ${STATE}" >&2
fi
