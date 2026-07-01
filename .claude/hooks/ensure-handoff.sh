#!/usr/bin/env bash
# ensure-handoff.sh — Stop hook. If this turn changed the repo but the handoff is
# stale relative to those changes, ask Claude to refresh CURRENT_SESSION.md before ending.
# Never loops (respects stop_hook_active). Never blocks read-only / no-change turns.
set -u

ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -z "${ROOT}" ] && ROOT="$(pwd)"
HANDOFF_REL="docs/handoffs/CURRENT_SESSION.md"
HANDOFF="${ROOT}/${HANDOFF_REL}"

PAYLOAD="$(cat 2>/dev/null || true)"

# Recursion guard: if we already blocked once this Stop chain, allow the stop.
ACTIVE="$(printf '%s' "${PAYLOAD}" | python3 -c "import sys,json;
try: print('1' if json.load(sys.stdin).get('stop_hook_active') else '0')
except Exception: print('0')" 2>/dev/null)"
[ "${ACTIVE}" = "1" ] && exit 0

# Not a git repo → nothing to guard.
git -C "${ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Decide staleness in Python: null-delimited porcelain handles spaces/quotes safely.
VERDICT="$(ROOT="${ROOT}" HANDOFF_REL="${HANDOFF_REL}" python3 <<'PY'
import os, subprocess, sys

root = os.environ["ROOT"]
handoff_rel = os.environ["HANDOFF_REL"]
handoff = os.path.join(root, handoff_rel)

def git(*args):
    return subprocess.run(["git", "-C", root, *args],
                          capture_output=True, text=True).stdout

# Changed paths (staged + unstaged + untracked), excluding our own bookkeeping files.
raw = git("status", "--porcelain", "-z")
ignore_prefixes = ("docs/handoffs/", ".claude/state/")
changed = []
for entry in raw.split("\0"):
    if not entry:
        continue
    path = entry[3:]  # strip "XY " status prefix
    if not path:
        continue
    if path.startswith(ignore_prefixes):
        continue
    changed.append(path)

# No meaningful changes → do not block (read-only / QA / handoff-only turn).
if not changed:
    print("ok"); sys.exit(0)

# Repo changed but handoff missing → must create it.
if not os.path.exists(handoff):
    print("missing"); sys.exit(0)

# Freshest change vs handoff mtime. If handoff is at least as new, it is fresh.
def mtime(p):
    try: return os.path.getmtime(p)
    except OSError: return 0.0

newest = 0.0
for p in changed:
    newest = max(newest, mtime(os.path.join(root, p)))

print("fresh" if mtime(handoff) >= newest else "stale")
PY
)"

case "${VERDICT}" in
  ok|fresh)
    exit 0
    ;;
  missing)
    MSG="Repository changed this turn but ${HANDOFF_REL} does not exist. Create it from docs/handoffs/TEMPLATE.md — fill Deliverable, Branch/commit, Working tree, and the exact Next step — before ending."
    ;;
  stale|*)
    MSG="Repository changed this turn but ${HANDOFF_REL} is older than the changed files. Update the handoff (Working tree, Files changed, Next step) so a fresh session can continue, before ending."
    ;;
esac

MSG="${MSG}" python3 <<'PY'
import os, json
print(json.dumps({"decision": "block", "reason": os.environ["MSG"]}, ensure_ascii=False))
PY
exit 0
