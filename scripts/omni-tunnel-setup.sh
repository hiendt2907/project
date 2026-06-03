#!/usr/bin/env bash
# Expose Omni Gateway qua Cloudflare Tunnel.
#
# Prerequisite:
#   1. Cloudflare account + domain đã add vào Cloudflare
#   2. brew install cloudflared    (script tự install nếu thiếu)
#
# Usage:
#   bash scripts/omni-tunnel-setup.sh --domain omni-gateway.yourdomain.com
#
# Kết quả:
#   - Cloudflare Tunnel "omni-gateway" được tạo
#   - DNS CNAME omni-gateway.yourdomain.com → tunnel
#   - LaunchAgent chạy port-forward + cloudflared khi Mac boot
#   - Remote agents có thể kết nối qua https://omni-gateway.yourdomain.com

set -euo pipefail

DOMAIN=""
TUNNEL_NAME="omni-gateway"
NS="multi-agent"
LOCAL_PORT=18080   # port-forward local port (tránh conflict với port 8080)
GW_SVC="omni-gateway"
PLIST_DIR="$HOME/Library/LaunchAgents"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${GREEN}[omni-tunnel]${NC} $*"; }
info() { echo -e "${CYAN}[omni-tunnel]${NC} $*"; }
warn() { echo -e "${YELLOW}[omni-tunnel]${NC} $*"; }
err()  { echo -e "${RED}[omni-tunnel]${NC} $*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --tunnel-name) TUNNEL_NAME="$2"; shift 2 ;;
    --port)   LOCAL_PORT="$2"; shift 2 ;;
    *) err "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$DOMAIN" ]]; then
  err "Missing --domain. Example: --domain omni-gateway.yourdomain.com"
  exit 1
fi

log "Omni Gateway Cloudflare Tunnel Setup"
log "  Domain:  $DOMAIN"
log "  Tunnel:  $TUNNEL_NAME"
log "  Port:    localhost:${LOCAL_PORT} → K8s ${GW_SVC}"

# ─── Step 1: Install cloudflared ──────────────────────────────────────────────
if ! command -v cloudflared &>/dev/null; then
  log "[1/6] Installing cloudflared via Homebrew..."
  brew install cloudflared
else
  log "[1/6] cloudflared already installed ($(cloudflared --version 2>&1 | head -1)) ✓"
fi

# ─── Step 2: Login to Cloudflare ──────────────────────────────────────────────
if [[ ! -f "$HOME/.cloudflared/cert.pem" ]]; then
  log "[2/6] Login to Cloudflare (browser sẽ mở)..."
  cloudflared tunnel login
else
  log "[2/6] Cloudflare credentials found ✓"
fi

# ─── Step 3: Create tunnel ────────────────────────────────────────────────────
log "[3/6] Tạo tunnel '$TUNNEL_NAME'..."
EXISTING=$(cloudflared tunnel list 2>/dev/null | grep "$TUNNEL_NAME" | awk '{print $1}' || true)
if [[ -n "$EXISTING" ]]; then
  TUNNEL_ID="$EXISTING"
  log "  Tunnel đã tồn tại: $TUNNEL_ID ✓"
else
  cloudflared tunnel create "$TUNNEL_NAME"
  TUNNEL_ID=$(cloudflared tunnel list 2>/dev/null | grep "$TUNNEL_NAME" | awk '{print $1}')
  log "  Tunnel created: $TUNNEL_ID"
fi

# ─── Step 4: Write cloudflared config ─────────────────────────────────────────
log "[4/6] Ghi config ~/.cloudflared/omni-gateway.yml..."
mkdir -p "$HOME/.cloudflared"
cat > "$HOME/.cloudflared/omni-gateway.yml" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $HOME/.cloudflared/${TUNNEL_ID}.json

ingress:
  - hostname: $DOMAIN
    service: http://127.0.0.1:${LOCAL_PORT}
    originRequest:
      noTLSVerify: false
      connectTimeout: 10s
  - service: http_status:404
EOF
log "  Config: ~/.cloudflared/omni-gateway.yml ✓"

# ─── Step 5: Create DNS CNAME ─────────────────────────────────────────────────
log "[5/6] Tạo DNS CNAME $DOMAIN → tunnel..."
cloudflared tunnel route dns "$TUNNEL_NAME" "$DOMAIN" || \
  warn "  DNS route có thể đã tồn tại — tiếp tục"
log "  DNS: $DOMAIN → ${TUNNEL_ID}.cfargotunnel.com ✓"

# ─── Step 6: Install LaunchAgents (Mac autostart) ─────────────────────────────
log "[6/6] Cài LaunchAgents để tự chạy khi boot..."
mkdir -p "$PLIST_DIR"

# 6a. LaunchAgent: kubectl port-forward
cat > "$PLIST_DIR/com.omni.gateway-portforward.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.omni.gateway-portforward</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/kubectl</string>
    <string>port-forward</string>
    <string>--address</string>
    <string>127.0.0.1</string>
    <string>svc/${GW_SVC}</string>
    <string>${LOCAL_PORT}:80</string>
    <string>-n</string>
    <string>${NS}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/omni-portforward.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/omni-portforward-err.log</string>
  <key>ThrottleInterval</key>
  <integer>5</integer>
</dict>
</plist>
EOF

# 6b. LaunchAgent: cloudflared tunnel
CLOUDFLARED_BIN=$(which cloudflared)
cat > "$PLIST_DIR/com.omni.cloudflare-tunnel.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.omni.cloudflare-tunnel</string>
  <key>ProgramArguments</key>
  <array>
    <string>${CLOUDFLARED_BIN}</string>
    <string>tunnel</string>
    <string>--config</string>
    <string>${HOME}/.cloudflared/omni-gateway.yml</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/omni-tunnel.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/omni-tunnel-err.log</string>
  <key>ThrottleInterval</key>
  <integer>5</integer>
</dict>
</plist>
EOF

# Load LaunchAgents
launchctl unload "$PLIST_DIR/com.omni.gateway-portforward.plist" 2>/dev/null || true
launchctl unload "$PLIST_DIR/com.omni.cloudflare-tunnel.plist" 2>/dev/null || true
launchctl load "$PLIST_DIR/com.omni.gateway-portforward.plist"
launchctl load "$PLIST_DIR/com.omni.cloudflare-tunnel.plist"
log "  LaunchAgents loaded ✓"

# ─── Verify ───────────────────────────────────────────────────────────────────
log "Chờ tunnel khởi động (10s)..."
sleep 10

info ""
info "Kiểm tra port-forward:"
info "  curl -s http://localhost:${LOCAL_PORT}/healthz"
HEALTH=$(curl -sf --max-time 5 "http://localhost:${LOCAL_PORT}/healthz" 2>/dev/null || echo "pending")
info "  → $HEALTH"

info ""
info "Kiểm tra tunnel từ internet:"
info "  curl -s https://${DOMAIN}/healthz"

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo " Omni Gateway — Cloudflare Tunnel Active"
echo "======================================================="
echo ""
echo " Public URL:  https://${DOMAIN}"
echo " Tunnel ID:   ${TUNNEL_ID}"
echo " Local port:  localhost:${LOCAL_PORT} → K8s ${GW_SVC}"
echo ""
echo " Dùng URL này khi cài agent:"
echo ""
echo "   sudo bash omni-agent-1.0.0/install.sh \\"
echo "     --gateway-url https://${DOMAIN} \\"
echo "     --api-key <KEY>"
echo ""
echo " Logs:"
echo "   tail -f /tmp/omni-portforward.log"
echo "   tail -f /tmp/omni-tunnel.log"
echo ""
echo " Dừng tunnel:"
echo "   launchctl unload ~/Library/LaunchAgents/com.omni.cloudflare-tunnel.plist"
echo "   launchctl unload ~/Library/LaunchAgents/com.omni.gateway-portforward.plist"
echo ""
echo " Xóa tunnel hoàn toàn:"
echo "   bash scripts/omni-tunnel-teardown.sh"
echo "======================================================="
