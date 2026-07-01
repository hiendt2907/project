#!/usr/bin/env bash
# load-session-context.sh — SessionStart hook. Injects repo-truth context into a new session.
# Emits JSON with hookSpecificOutput.additionalContext. Never dumps the transcript.
set -u

ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -z "${ROOT}" ] && ROOT="$(pwd)"
HANDOFF="${ROOT}/docs/handoffs/CURRENT_SESSION.md"
STATE="${ROOT}/.claude/state/last-session.json"

# Bound the handoff we inject so a bloated handoff cannot flood the new session.
MAX_HANDOFF_BYTES=8000

PAYLOAD="$(cat 2>/dev/null || true)"
SOURCE="$(printf '%s' "${PAYLOAD}" | python3 -c "import sys,json;
try: print(json.load(sys.stdin).get('source',''))
except Exception: print('')" 2>/dev/null)"
[ -z "${SOURCE}" ] && SOURCE="unknown"

emit() {
  # $1 = context markdown. Wrap into SessionStart additionalContext JSON.
  CTX="$1" python3 <<'PY'
import os, json
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": os.environ.get("CTX", ""),
    }
}, ensure_ascii=False))
PY
}

CTX="# Session bootstrap (auto-loaded from repository artifacts)\n\n"
CTX="${CTX}Session source: ${SOURCE}\n"
CTX="${CTX}Repository path: ${ROOT}\n"

if git -C "${ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  BRANCH="$(git -C "${ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  HEAD_COMMIT="$(git -C "${ROOT}" log --oneline -1 2>/dev/null)"
  STATUS="$(git -C "${ROOT}" status --short 2>/dev/null | head -40)"
  CTX="${CTX}Git branch: ${BRANCH}\n"
  CTX="${CTX}HEAD: ${HEAD_COMMIT}\n"
  CTX="${CTX}\n## Working tree (git status --short)\n\`\`\`\n${STATUS:-clean}\n\`\`\`\n"
else
  CTX="${CTX}Git: not a git repository\n"
fi

# --- Handoff ---
CTX="${CTX}\n## Current handoff (docs/handoffs/CURRENT_SESSION.md)\n"
if [ -f "${HANDOFF}" ]; then
  SIZE="$(wc -c < "${HANDOFF}" 2>/dev/null | tr -d ' ')"
  if [ "${SIZE:-0}" -gt "${MAX_HANDOFF_BYTES}" ]; then
    CTX="${CTX}> WARNING: handoff is ${SIZE} bytes (> ${MAX_HANDOFF_BYTES}); truncated. Trim it via /prepare-clear.\n"
    BODY="$(head -c "${MAX_HANDOFF_BYTES}" "${HANDOFF}" 2>/dev/null)"
  else
    BODY="$(cat "${HANDOFF}" 2>/dev/null)"
  fi
  CTX="${CTX}\n${BODY}\n"
else
  CTX="${CTX}> No handoff found. Create docs/handoffs/CURRENT_SESSION.md (see TEMPLATE.md) before checkpointing.\n"
fi

# --- Last-session metadata (safe subset) ---
if [ -f "${STATE}" ]; then
  META="$(STATE="${STATE}" python3 <<'PY'
import os, json
try:
    d = json.load(open(os.environ["STATE"]))
except Exception:
    print(""); raise SystemExit
keep = ("timestamp", "event", "reason", "branch", "head_commit", "current_deliverable")
lines = [f"- {k}: {d[k]}" for k in keep if d.get(k)]
print("\n".join(lines))
PY
)"
  [ -n "${META}" ] && CTX="${CTX}\n## Last session snapshot (.claude/state/last-session.json)\n${META}\n"
fi

# --- Continuation rules ---
CTX="${CTX}\n## Continuation rules\n"
CTX="${CTX}- Repository artifacts are the source of truth; conversation history is not.\n"
CTX="${CTX}- Verify Git state before editing.\n"
CTX="${CTX}- Read only files the handoff references; do not scan the whole repo.\n"
CTX="${CTX}- Continue from the exact Next step in the handoff.\n"
CTX="${CTX}- Do not redesign anything recorded as a settled decision.\n"
CTX="${CTX}- Do not infer history from old conversations.\n"
CTX="${CTX}- If Git state contradicts the handoff, STOP and report the conflict before coding.\n"

# Bash \n are literal; printf %b turns them into real newlines while preserving UTF-8.
FINAL="$(printf '%b' "${CTX}")"
[ -z "${FINAL}" ] && FINAL="${CTX}"
emit "${FINAL}"
exit 0
