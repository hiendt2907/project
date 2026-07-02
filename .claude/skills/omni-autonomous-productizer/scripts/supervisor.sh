#!/usr/bin/env bash
# External supervisor for the omni-autonomous-productizer loop. Pure
# orchestration only: inspect state -> sleep until quota reset -> invoke
# `claude -p "/omni-autonomous-productizer resume|one-iteration"` -> repeat.
# The supervisor process itself never edits code, deploys, deletes
# resources, enables auto-execute, commits, or pushes — those only ever
# happen inside the invoked `claude -p` process.
#
# ⚠️  SAFETY OVERRIDE (explicit, twice-confirmed user request, 2026-07-02):
# `claude -p` runs non-interactively (no TTY), so without a permission
# override every Edit/Write/Bash call is auto-denied and the loop hot-loops
# doing nothing (observed live: iterations 7+ all blocked on
# src/gateway/routes/autonomy.py, no progress). The user was shown the
# safer alternative (scoped --allowedTools allowlist) and explicitly chose
# full --dangerously-skip-permissions instead, twice, after the tradeoff was
# explained. This CONTRADICTS the general project rule "never use
# --dangerously-skip-permissions" — it is enabled here ONLY because of that
# explicit override, not by default project policy. If you did not
# personally ask for this, do not re-enable it after disabling.
# OMNI_AUTO_EXECUTE_ENABLED=false remains a SEPARATE, unrelated safety gate
# inside Omni itself (K8s mutation kill-switch) — this override does not
# touch it and never should.
#
# Usage:
#   scripts/supervisor.sh --start   # run the supervise loop in the foreground
#   scripts/supervisor.sh --status  # print whether a supervisor is running
#   scripts/supervisor.sh --stop    # stop a running supervisor safely
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
STATE="${PROJECT_ROOT}/docs/operations/AUTONOMOUS_LOOP_STATE.json"
LOCK_DIR="${PROJECT_ROOT}/.autonomous-loop"
LOCK_FILE="${LOCK_DIR}/supervisor.lock"
LOG_DIR="${LOCK_DIR}/logs"
CLAUDE_FLAGS=(--dangerously-skip-permissions)

mkdir -p "$LOG_DIR"

log() { printf '[supervisor %s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "${LOG_DIR}/supervisor.log"; }

cleanup() {
  rm -f "$LOCK_FILE"
}

acquire_lock() {
  if [[ -f "$LOCK_FILE" ]]; then
    local pid
    pid="$(cat "$LOCK_FILE" 2>/dev/null || echo "")"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      log "ERROR: another supervisor already running (pid=$pid). Refusing to start a second instance."
      exit 1
    fi
    log "stale lock file found (pid=$pid not running) — removing"
    rm -f "$LOCK_FILE"
  fi
  echo "$$" > "$LOCK_FILE"
  trap cleanup EXIT INT TERM
}

status() {
  if [[ -f "$LOCK_FILE" ]]; then
    local pid
    pid="$(cat "$LOCK_FILE" 2>/dev/null || echo "")"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "running (pid=$pid)"
      return 0
    fi
  fi
  echo "not running"
}

stop() {
  if [[ -f "$LOCK_FILE" ]]; then
    local pid
    pid="$(cat "$LOCK_FILE" 2>/dev/null || echo "")"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      log "stopping supervisor pid=$pid"
      kill "$pid"
      rm -f "$LOCK_FILE"
      return 0
    fi
  fi
  log "no running supervisor to stop"
}

require_claude_cli() {
  if ! command -v claude >/dev/null 2>&1; then
    log "ERROR: 'claude' CLI not found on PATH — cannot invoke resume."
    exit 1
  fi
}

caffeinate_wrap() {
  # macOS-only: avoid the OS sleeping during a long wait. No-op elsewhere.
  if command -v caffeinate >/dev/null 2>&1; then
    caffeinate -i -w $$ &
  fi
}

supervise_loop() {
  require_claude_cli
  acquire_lock
  caffeinate_wrap

  local backoff=30
  local max_backoff=1800

  while true; do
    if [[ ! -f "$STATE" ]]; then
      log "ERROR: state file not found at $STATE — stopping"
      exit 1
    fi

    if ! python3 "${SCRIPT_DIR}/validate_state.py" --state-file "$STATE" >>"${LOG_DIR}/supervisor.log" 2>&1; then
      log "ERROR: state file failed validation — stopping (see supervisor.log)"
      exit 1
    fi

    local status_val
    status_val="$(python3 -c "import json,sys; print(json.load(open('$STATE'))['status'])")"
    log "current status=$status_val"

    case "$status_val" in
      SLEEPING_UNTIL_QUOTA_RESET)
        log "sleeping until quota reset..."
        if python3 "${SCRIPT_DIR}/calculate_sleep.py" --state-file "$STATE" --sleep; then
          backoff=30
        fi
        log "invoking resume (${CLAUDE_FLAGS[*]})"
        if claude -p "/omni-autonomous-productizer resume" "${CLAUDE_FLAGS[@]}" >>"${LOG_DIR}/supervisor.log" 2>&1; then
          backoff=30
        else
          log "resume invocation failed — backing off ${backoff}s"
          sleep "$backoff"
          backoff=$(( backoff * 2 > max_backoff ? max_backoff : backoff * 2 ))
        fi
        # Unconditional pacing floor — a live session runs many API calls and
        # takes minutes in practice, but if something ever returns instantly
        # this stops the loop from hot-looping (and burning quota) on repeat.
        sleep 10
        ;;
      IDLE)
        # No live session mid-iteration and nothing to sleep for — this is
        # the unambiguous "kick the next iteration" state. one-iteration
        # performs exactly one vertical slice and checkpoints back to IDLE
        # (or QUOTA_DRAINING/SLEEPING_UNTIL_QUOTA_RESET) on its own; it never
        # opens a second iteration itself, so calling it repeatedly here is
        # what actually drives the 24/7 loop forward.
        log "status=IDLE — invoking one-iteration (${CLAUDE_FLAGS[*]})"
        if claude -p "/omni-autonomous-productizer one-iteration" "${CLAUDE_FLAGS[@]}" >>"${LOG_DIR}/supervisor.log" 2>&1; then
          backoff=30
        else
          log "one-iteration invocation failed — backing off ${backoff}s"
          sleep "$backoff"
          backoff=$(( backoff * 2 > max_backoff ? max_backoff : backoff * 2 ))
        fi
        sleep 10
        ;;
      BLOCKED_FOR_HUMAN)
        log "status=BLOCKED_FOR_HUMAN — supervisor exiting, human action required"
        exit 0
        ;;
      STOPPED|COMPLETED)
        log "status=$status_val — supervisor exiting"
        exit 0
        ;;
      *)
        # DISCOVERING..COMMITTING/QUOTA_DRAINING/RESUMING: ambiguous whether a
        # live session is actively mid-iteration right now or a prior session
        # crashed leaving this status stale. Never safe to auto-invoke here —
        # poll slowly and let a human or the next IDLE/SLEEPING transition
        # resolve it.
        sleep 60
        ;;
    esac
  done
}

case "${1:-}" in
  --start) supervise_loop ;;
  --status) status ;;
  --stop) stop ;;
  *) echo "Usage: $0 --start|--status|--stop" >&2; exit 1 ;;
esac
