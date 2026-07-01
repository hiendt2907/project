#!/usr/bin/env bash
# test_session_hooks.sh — behavioral tests for the Claude session-automation hooks.
# Runs each hook against synthetic hook payloads inside throwaway git repos.
# No secrets, no transcripts, no network. Exits non-zero if any assertion fails.
set -u

HOOKS_DIR="$(cd "$(dirname "$0")/../../.claude/hooks" && pwd)"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }

mkrepo() {
  local d; d="$(mktemp -d)"
  git -C "$d" init -q
  git -C "$d" config user.email t@t.t; git -C "$d" config user.name t
  mkdir -p "$d/docs/handoffs" "$d/.claude/state"
  echo "base" > "$d/README.md"
  git -C "$d" add -A; git -C "$d" commit -qm init
  echo "$d"
}
run() { # run <script> <root> <payload-json>
  printf '%s' "$3" | CLAUDE_PROJECT_DIR="$2" bash "${HOOKS_DIR}/$1" 2>/dev/null
}

echo "== A. save-session-state =="
R="$(mkrepo)"; cp docs/handoffs/TEMPLATE.md "$R/docs/handoffs/CURRENT_SESSION.md" 2>/dev/null || echo "## Deliverable
X" > "$R/docs/handoffs/CURRENT_SESSION.md"
SECRET_TOKEN="sk-leak-should-not-appear" \
  run save-session-state.sh "$R" '{"hook_event_name":"SessionEnd","reason":"exit"}' >/dev/null
S="$R/.claude/state/last-session.json"
[ -f "$S" ] && ok "state file created" || bad "state file created"
python3 -c "import json;json.load(open('$S'))" 2>/dev/null && ok "valid JSON" || bad "valid JSON"
grep -q "$(git -C "$R" rev-parse --short HEAD)" "$S" && ok "records HEAD commit" || bad "records HEAD commit"
python3 -c "import json;d=json.load(open('$S'));import sys;sys.exit(0 if d.get('branch') else 1)" && ok "records branch" || bad "records branch"
grep -q "sk-leak" "$S" && bad "no secret leak" || ok "no secret leak"
grep -q "reason" "$S" && ok "records reason" || bad "records reason"
rm -rf "$R"

echo "== A2. atomic write / not-a-git-repo =="
D="$(mktemp -d)"  # plain dir, no git
run save-session-state.sh "$D" '{"hook_event_name":"SessionEnd"}' >/dev/null
python3 -c "import json;d=json.load(open('$D/.claude/state/last-session.json'));import sys;sys.exit(0 if d.get('git') is False else 1)" 2>/dev/null \
  && ok "non-git fails gracefully" || bad "non-git fails gracefully"
ls "$D/.claude/state/".*.tmp >/dev/null 2>&1 && bad "no leftover tmp" || ok "no leftover tmp"
rm -rf "$D"

echo "== B. load-session-context =="
R="$(mkrepo)"; echo "## Deliverable
My deliverable
## Next step chính xác
Do the thing" > "$R/docs/handoffs/CURRENT_SESSION.md"
OUT="$(run load-session-context.sh "$R" '{"hook_event_name":"SessionStart","source":"clear"}')"
echo "$OUT" | python3 -c "import sys,json;d=json.load(sys.stdin);assert 'My deliverable' in d['hookSpecificOutput']['additionalContext']" 2>/dev/null && ok "injects handoff content" || bad "injects handoff content"
echo "$OUT" | grep -q "$(git -C "$R" rev-parse --abbrev-ref HEAD)" && ok "injects branch" || bad "injects branch"
echo "$OUT" | grep -q "source: clear" && ok "injects session source" || bad "injects session source"
echo "$OUT" | grep -qi "transcript" && bad "no transcript word" || ok "no transcript injected"
rm -rf "$R"

echo "== B2. load: missing state + missing handoff =="
R="$(mkrepo)"  # no handoff, no state
OUT="$(run load-session-context.sh "$R" '{"hook_event_name":"SessionStart","source":"startup"}')"
echo "$OUT" | python3 -c "import sys,json;json.load(sys.stdin)" 2>/dev/null && ok "valid JSON without handoff/state" || bad "valid JSON without handoff/state"
echo "$OUT" | grep -qi "No handoff found" && ok "warns missing handoff" || bad "warns missing handoff"
rm -rf "$R"

echo "== B3. load: handoff too long warns =="
R="$(mkrepo)"; python3 -c "open('$R/docs/handoffs/CURRENT_SESSION.md','w').write('## Deliverable\n'+'x'*9000)"
OUT="$(run load-session-context.sh "$R" '{"hook_event_name":"SessionStart","source":"resume"}')"
echo "$OUT" | grep -qi "WARNING: handoff" && ok "warns on oversized handoff" || bad "warns on oversized handoff"
rm -rf "$R"

echo "== C. ensure-handoff =="
# C1: clean repo, no changes → no block
R="$(mkrepo)"; echo "h" > "$R/docs/handoffs/CURRENT_SESSION.md"; git -C "$R" add -A; git -C "$R" commit -qm h
OUT="$(run ensure-handoff.sh "$R" '{"hook_event_name":"Stop","stop_hook_active":false}')"
[ -z "$OUT" ] && ok "clean repo does not block" || bad "clean repo does not block"
# C2: repo changed, handoff missing → block
echo "new code" > "$R/feature.py"
OUT="$(run ensure-handoff.sh "$R" '{"hook_event_name":"Stop","stop_hook_active":false}')"
echo "$OUT" | grep -q '"decision": "block"' && ok "change + no fresh handoff blocks" || bad "change + no fresh handoff blocks"
# C3: handoff refreshed after change → no block
sleep 1; echo "updated" > "$R/docs/handoffs/CURRENT_SESSION.md"
OUT="$(run ensure-handoff.sh "$R" '{"hook_event_name":"Stop","stop_hook_active":false}')"
[ -z "$OUT" ] && ok "fresh handoff does not block" || bad "fresh handoff does not block"
# C4: recursion guard
echo "more" > "$R/feature2.py"
OUT="$(run ensure-handoff.sh "$R" '{"hook_event_name":"Stop","stop_hook_active":true}')"
[ -z "$OUT" ] && ok "stop_hook_active prevents loop" || bad "stop_hook_active prevents loop"
rm -rf "$R"

echo "== D. git edge cases =="
# detached HEAD
R="$(mkrepo)"; echo "x">"$R/a.txt"; git -C "$R" add -A; git -C "$R" commit -qm c2
git -C "$R" checkout -q "$(git -C "$R" rev-parse HEAD~1)"
run save-session-state.sh "$R" '{"hook_event_name":"PreCompact","trigger":"manual"}' >/dev/null
python3 -c "import json;json.load(open('$R/.claude/state/last-session.json'))" && ok "detached HEAD ok" || bad "detached HEAD ok"
rm -rf "$R"
# filename with spaces
R="$(mkrepo)"; echo "h">"$R/docs/handoffs/CURRENT_SESSION.md"; git -C "$R" add -A; git -C "$R" commit -qm h
sleep 1; echo "z" > "$R/a file with spaces.py"
OUT="$(run ensure-handoff.sh "$R" '{"hook_event_name":"Stop","stop_hook_active":false}')"
echo "$OUT" | grep -q '"decision": "block"' && ok "spaces in filename handled (blocks stale)" || bad "spaces in filename handled"
rm -rf "$R"

echo
echo "RESULT: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
