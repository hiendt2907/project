#!/usr/bin/env bash
# save-session-state.sh — snapshot safe Git/session metadata on PreCompact + SessionEnd.
# Reads hook JSON payload from stdin. Writes .claude/state/last-session.json atomically.
# NEVER logs secrets, env vars, transcript, or prompts. Fails gracefully outside git.
set -u

ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -z "${ROOT}" ] && ROOT="$(pwd)"
STATE_DIR="${ROOT}/.claude/state"
OUT="${STATE_DIR}/last-session.json"
HANDOFF_REL="docs/handoffs/CURRENT_SESSION.md"
HANDOFF="${ROOT}/${HANDOFF_REL}"

# Read hook payload (safe fields only). Never echo it back.
PAYLOAD="$(cat 2>/dev/null || true)"

json_field() {
  # $1 = key. Extract a top-level string field from payload without leaking the rest.
  printf '%s' "${PAYLOAD}" | python3 -c "import sys,json;
try:
 d=json.load(sys.stdin); v=d.get('$1','')
 print(v if isinstance(v,str) else '')
except Exception:
 print('')" 2>/dev/null
}

# Not a git repo? Snapshot minimal metadata and exit 0 (never break the session).
if ! git -C "${ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  mkdir -p "${STATE_DIR}" 2>/dev/null || true
  printf '{"timestamp":"%s","git":false,"reason":"not-a-git-repo"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${OUT}" 2>/dev/null || true
  exit 0
fi

EVENT="$(json_field hook_event_name)"
REASON="$(json_field reason)"
[ -z "${REASON}" ] && REASON="$(json_field trigger)"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BRANCH="$(git -C "${ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null)"
HEAD_COMMIT="$(git -C "${ROOT}" rev-parse --short HEAD 2>/dev/null)"
STATUS="$(git -C "${ROOT}" status --porcelain 2>/dev/null | head -c 4000)"
RECENT="$(git -C "${ROOT}" log --oneline -5 2>/dev/null)"

# Handoff checksum + current deliverable line (safe: our own artifact).
HANDOFF_SUM=""
DELIVERABLE=""
if [ -f "${HANDOFF}" ]; then
  HANDOFF_SUM="$(shasum -a 256 "${HANDOFF}" 2>/dev/null | awk '{print $1}')"
  DELIVERABLE="$(awk '/^## Deliverable/{f=1;next} f&&NF{print;exit}' "${HANDOFF}" 2>/dev/null | head -c 300)"
fi

mkdir -p "${STATE_DIR}" 2>/dev/null || true
TMP="$(mktemp "${STATE_DIR}/.last-session.XXXXXX.tmp" 2>/dev/null)" || TMP="${OUT}.tmp"

TS="${TS}" EVENT="${EVENT}" REASON="${REASON}" BRANCH="${BRANCH}" \
HEAD_COMMIT="${HEAD_COMMIT}" STATUS="${STATUS}" RECENT="${RECENT}" \
HANDOFF_REL="${HANDOFF_REL}" HANDOFF_SUM="${HANDOFF_SUM}" DELIVERABLE="${DELIVERABLE}" \
python3 <<'PY' > "${TMP}" 2>/dev/null
import os, json
data = {
    "timestamp": os.environ.get("TS", ""),
    "git": True,
    "event": os.environ.get("EVENT", ""),
    "reason": os.environ.get("REASON", ""),
    "branch": os.environ.get("BRANCH", ""),
    "head_commit": os.environ.get("HEAD_COMMIT", ""),
    "status_short": os.environ.get("STATUS", ""),
    "recent_commits": [l for l in os.environ.get("RECENT", "").splitlines() if l],
    "handoff_path": os.environ.get("HANDOFF_REL", ""),
    "handoff_checksum": os.environ.get("HANDOFF_SUM", ""),
    "current_deliverable": os.environ.get("DELIVERABLE", ""),
}
print(json.dumps(data, ensure_ascii=False, indent=2))
PY

# Validate JSON before promoting the temp file. On failure, keep old state intact.
if [ -s "${TMP}" ] && python3 -c "import json,sys; json.load(open('${TMP}'))" >/dev/null 2>&1; then
  mv -f "${TMP}" "${OUT}" 2>/dev/null || true
else
  rm -f "${TMP}" 2>/dev/null || true
fi

exit 0
