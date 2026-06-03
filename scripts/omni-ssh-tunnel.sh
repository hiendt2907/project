#!/usr/bin/env bash
# Tạo SSH reverse tunnel từ Mac → customer server.
# Agent trên server kết nối qua localhost:REMOTE_PORT → tunnel → Omni gateway.
#
# Usage:
#   bash scripts/omni-ssh-tunnel.sh --host user@192.168.1.100 [options]
#
# Options:
#   --host SSH_TARGET      user@host hoặc SSH alias (required)
#   --remote-port PORT     Port lắng nghe trên customer server (default: 8899)
#   --local-port PORT      Port-forward local trên Mac (default: 18080)
#   --ssh-key PATH         SSH private key (default: ~/.ssh/id_rsa)
#   --persist              Cài LaunchAgent để tunnel tự chạy khi Mac boot
#   --stop                 Dừng và xóa LaunchAgent

set -euo pipefail

SSH_TARGET=""
REMOTE_PORT=8899
LOCAL_PORT=18080
SSH_KEY="$HOME/.ssh/id_rsa"
PERSIST=false
STOP=false
PLIST="$HOME/Library/LaunchAgents/com.omni.ssh-reverse-tunnel.plist"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${GREEN}[omni-tunnel]${NC} $*"; }
info() { echo -e "${CYAN}[omni-tunnel]${NC} $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)        SSH_TARGET="$2"; shift 2 ;;
    --remote-port) REMOTE_PORT="$2"; shift 2 ;;
    --local-port)  LOCAL_PORT="$2"; shift 2 ;;
    --ssh-key)     SSH_KEY="$2"; shift 2 ;;
    --persist)     PERSIST=true; shift ;;
    --stop)        STOP=true; shift ;;
    *) echo "Unknown: $1"; exit 1 ;;
  esac
done

# ─── Stop ─────────────────────────────────────────────────────────────────────
if $STOP; then
  launchctl unload "$PLIST" 2>/dev/null && log "Tunnel stopped" || log "Tunnel not running"
  rm -f "$PLIST"
  exit 0
fi

[[ -z "$SSH_TARGET" ]] && { echo "Missing --host"; exit 1; }

# ─── Check autossh ────────────────────────────────────────────────────────────
if ! command -v autossh &>/dev/null; then
  log "Installing autossh (giữ tunnel sống khi mạng bị gián đoạn)..."
  brew install autossh
fi

# ─── Test SSH ─────────────────────────────────────────────────────────────────
log "Kiểm tra SSH kết nối tới $SSH_TARGET..."
ssh -i "$SSH_KEY" -o ConnectTimeout=5 -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new \
    "$SSH_TARGET" "echo ok" &>/dev/null \
  && log "  SSH OK ✓" \
  || { echo "Không SSH được vào $SSH_TARGET. Kiểm tra key và host."; exit 1; }

# ─── Persist mode: LaunchAgent ────────────────────────────────────────────────
if $PERSIST; then
  AUTOSSH_BIN=$(which autossh)
  log "Cài LaunchAgent → tunnel tự chạy khi Mac boot..."
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.omni.ssh-reverse-tunnel</string>
  <key>ProgramArguments</key>
  <array>
    <string>${AUTOSSH_BIN}</string>
    <string>-M</string>
    <string>0</string>
    <string>-N</string>
    <string>-i</string>
    <string>${SSH_KEY}</string>
    <string>-o</string><string>ServerAliveInterval=30</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>StrictHostKeyChecking=accept-new</string>
    <string>-R</string>
    <string>127.0.0.1:${REMOTE_PORT}:127.0.0.1:${LOCAL_PORT}</string>
    <string>${SSH_TARGET}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>/tmp/omni-ssh-tunnel.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/omni-ssh-tunnel-err.log</string>
</dict>
</plist>
EOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  log "LaunchAgent loaded ✓"
  sleep 3
  log "Kiểm tra tunnel..."
  ssh -i "$SSH_KEY" -o ConnectTimeout=5 "$SSH_TARGET" \
    "ss -tlnp 2>/dev/null | grep ${REMOTE_PORT} || netstat -tlnp 2>/dev/null | grep ${REMOTE_PORT} || echo 'port check: ok (may need a moment)'"
else
  # Chạy foreground (Ctrl+C để dừng)
  log "Chạy tunnel (Ctrl+C để dừng)..."
  log "Trên server dùng: OMNI_AGENT_GATEWAY_URL=http://127.0.0.1:${REMOTE_PORT}"
  autossh -M 0 -N \
    -i "$SSH_KEY" \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=accept-new \
    -R "127.0.0.1:${REMOTE_PORT}:127.0.0.1:${LOCAL_PORT}" \
    "$SSH_TARGET"
fi

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo " SSH Reverse Tunnel Active"
echo "======================================================="
echo ""
echo " Mac port-forward : localhost:${LOCAL_PORT} → K8s omni-gateway"
echo " SSH tunnel       : ${SSH_TARGET} port ${REMOTE_PORT} → Mac"
echo ""
echo " Cài agent trên server với:"
echo ""
echo "   sudo bash omni-agent-1.0.0/install.sh \\"
echo "     --gateway-url http://127.0.0.1:${REMOTE_PORT} \\"
echo "     --api-key <KEY>"
echo ""
echo " Logs: tail -f /tmp/omni-ssh-tunnel.log"
echo " Stop: bash scripts/omni-ssh-tunnel.sh --stop"
echo "======================================================="
