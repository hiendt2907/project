#!/usr/bin/env bash
# Install LaunchAgent: omni-embed (:8001) on Mac (login session). vLLM đã gỡ — không cài :8000.
# Usage: bash scripts/install_mac_llm_launchagents.sh
# Remove embed (+ dọn plist vLLM cũ nếu còn): bash scripts/install_mac_llm_launchagents.sh --uninstall
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_ID="$(id -u)"
AGENT_DIR="${HOME}/Library/LaunchAgents"
LEGACY_VLLM_PLIST="${AGENT_DIR}/com.omni.mac.vllm.plist"
EMBED_PLIST="${AGENT_DIR}/com.omni.mac.embed.plist"

uninstall() {
  launchctl bootout "gui/${USER_ID}/com.omni.mac.vllm" 2>/dev/null || true
  launchctl bootout "gui/${USER_ID}/com.omni.mac.embed" 2>/dev/null || true
  rm -f "$LEGACY_VLLM_PLIST" "$EMBED_PLIST"
  echo "Removed LaunchAgents: com.omni.mac.vllm (legacy), com.omni.mac.embed"
  exit 0
}

[[ "${1:-}" == "--uninstall" ]] && uninstall

chmod +x "${REPO}/scripts/launchd/omni-embed-m4.sh"
mkdir -p "$AGENT_DIR"

# Dọn plist vLLM cũ (không chạy script cài vLLM nữa).
launchctl bootout "gui/${USER_ID}/com.omni.mac.vllm" 2>/dev/null || true
rm -f "$LEGACY_VLLM_PLIST"

cat >"$EMBED_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.omni.mac.embed</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${REPO}/scripts/launchd/omni-embed-m4.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>${TMPDIR:-/tmp}/omni-embed-launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${TMPDIR:-/tmp}/omni-embed-launchd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/bin:/bin</string>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>OMNI_REPO_ROOT</key>
    <string>${REPO}</string>
  </dict>
</dict>
</plist>
EOF

launchctl bootout "gui/${USER_ID}/com.omni.mac.embed" 2>/dev/null || true
launchctl bootstrap "gui/${USER_ID}" "$EMBED_PLIST"
launchctl enable "gui/${USER_ID}/com.omni.mac.embed"
launchctl kickstart -k "gui/${USER_ID}/com.omni.mac.embed"

echo "Installed: com.omni.mac.embed → :8001 (logs: ${TMPDIR:-/tmp}/omni-embed-launchd.*.log)"
echo "Legacy com.omni.mac.vllm plist removed if present."
