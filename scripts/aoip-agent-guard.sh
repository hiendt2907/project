#!/usr/bin/env bash
# Crash-loop guard cho AOIP safe update (Sprint NV-SRE IT-5).
#
# Chạy ở ExecStartPre của aoip-agent.service — CỐ Ý là shell NGOÀI bundle được
# hash: nếu bundle mới hỏng đến mức Python không boot nổi thì startup_gate
# (Python) không bao giờ chạy, chỉ script này còn cứu được.
#
# Logic: có pending.json (update chưa qua health-gate) → đếm boot. Quá
# MAX_BOOT_ATTEMPTS → restore bundle N-1 (previous.tar.gz) + ghi result.json
# rolled_back để reconciler báo outcome về Omni. Không có pending → dọn bootcount.
#
# Layout phải khớp src/aoip/agent/updater.py (_PENDING_MARKER/_RESULT_MARKER/
# _BOOTCOUNT_FILE/_PREVIOUS_BUNDLE/_BUNDLE_PACKAGES).
set -u

INSTALL_DIR="${OMNI_AGENT_INSTALL_DIR:-/opt/omni-remote-agent}"
RELEASES_DIR="${AOIP_RELEASES_DIR:-/var/lib/aoip/releases}"
MAX_BOOT_ATTEMPTS=3

PENDING="$RELEASES_DIR/pending.json"
RESULT="$RELEASES_DIR/result.json"
BOOTCOUNT="$RELEASES_DIR/bootcount"
BACKUP="$RELEASES_DIR/previous.tar.gz"

# Boot thường (không có update dở dang) → dọn counter, xong.
if [ ! -f "$PENDING" ]; then
  rm -f "$BOOTCOUNT"
  exit 0
fi

n=$(cat "$BOOTCOUNT" 2>/dev/null || echo 0)
case "$n" in *[!0-9]*|'') n=0 ;; esac
n=$((n + 1))
echo "$n" > "$BOOTCOUNT"

if [ "$n" -le "$MAX_BOOT_ATTEMPTS" ]; then
  exit 0  # cho Python startup_gate cơ hội tự commit/rollback
fi

echo "[aoip-guard] boot attempt $n > $MAX_BOOT_ATTEMPTS with pending update — rolling back" >&2
restored=false
if [ -f "$BACKUP" ]; then
  rm -rf "$INSTALL_DIR/remote_agent" "$INSTALL_DIR/aoip"
  if tar -xzf "$BACKUP" -C "$INSTALL_DIR"; then
    restored=true
  fi
fi

version=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$PENDING" 2>/dev/null || echo "")
command_id=$(sed -n 's/.*"command_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$PENDING" 2>/dev/null || echo "")
printf '{"update_status":"rolled_back","version":"%s","command_id":"%s","detail":"crash_loop_guard restored=%s","needs_restart":false}\n' \
  "$version" "$command_id" "$restored" > "$RESULT"
rm -f "$PENDING" "$BOOTCOUNT"
exit 0
