#!/usr/bin/env bash
# Xóa Cloudflare Tunnel và LaunchAgents cho Omni Gateway.
set -euo pipefail

TUNNEL_NAME="${1:-omni-gateway}"
PLIST_DIR="$HOME/Library/LaunchAgents"

echo "Stopping LaunchAgents..."
launchctl unload "$PLIST_DIR/com.omni.cloudflare-tunnel.plist" 2>/dev/null && \
  echo "  tunnel stopped" || echo "  (not running)"
launchctl unload "$PLIST_DIR/com.omni.gateway-portforward.plist" 2>/dev/null && \
  echo "  port-forward stopped" || echo "  (not running)"

echo "Removing LaunchAgent plists..."
rm -f "$PLIST_DIR/com.omni.cloudflare-tunnel.plist"
rm -f "$PLIST_DIR/com.omni.gateway-portforward.plist"

echo "Deleting Cloudflare tunnel '$TUNNEL_NAME'..."
TUNNEL_ID=$(cloudflared tunnel list 2>/dev/null | grep "$TUNNEL_NAME" | awk '{print $1}' || true)
if [[ -n "$TUNNEL_ID" ]]; then
  cloudflared tunnel delete "$TUNNEL_NAME" && echo "  deleted: $TUNNEL_ID"
else
  echo "  tunnel not found"
fi

echo "Done. DNS CNAME record phải xóa thủ công trong Cloudflare dashboard."
