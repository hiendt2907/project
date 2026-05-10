#!/usr/bin/env bash
# Verify Telegram bot credentials and outbound network from inside the omni-worker pod.
# Usage: ./scripts/check_connectivity.sh [namespace]
set -euo pipefail

NAMESPACE="${1:-multi-agent}"
POD=$(kubectl -n "$NAMESPACE" get pod -l app=omni-worker -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

if [[ -z "$POD" ]]; then
  echo "ERROR: No omni-worker pod found in namespace $NAMESPACE"
  exit 1
fi

echo "=== Checking pod: $POD in $NAMESPACE ==="

# 1. Print resolved Telegram env vars (masked)
echo ""
echo "--- Telegram env vars ---"
kubectl -n "$NAMESPACE" exec "$POD" -- sh -c '
  token=${TELEGRAM_BOT_TOKEN:-}
  chat_id=${TELEGRAM_CHAT_ID:-}
  admin_id=${OMNI_TELEGRAM_ADMIN_CHAT_ID:-}
  echo "TELEGRAM_BOT_TOKEN  : ${token:0:8}...${token: -4} (len=${#token})"
  echo "TELEGRAM_CHAT_ID    : $chat_id"
  echo "OMNI_TELEGRAM_ADMIN_CHAT_ID: $admin_id"
'

# 2. DNS resolution
echo ""
echo "--- DNS resolution for api.telegram.org ---"
kubectl -n "$NAMESPACE" exec "$POD" -- sh -c 'nslookup api.telegram.org 2>&1 || host api.telegram.org 2>&1 || echo "nslookup/host not available"'

# 3. getMe API call
echo ""
echo "--- Telegram getMe (validates token + network) ---"
kubectl -n "$NAMESPACE" exec "$POD" -- sh -c '
  token=${TELEGRAM_BOT_TOKEN:-}
  if [[ -z "$token" ]]; then
    echo "FAIL: TELEGRAM_BOT_TOKEN is empty"
    exit 1
  fi
  curl -s --max-time 10 "https://api.telegram.org/bot${token}/getMe" | python3 -c "
import sys, json
r = json.load(sys.stdin)
if r.get(\"ok\"):
    b = r[\"result\"]
    print(f\"OK: bot={b[\"username\"]} id={b[\"id\"]}\")
else:
    print(f\"FAIL: {r}\")
    sys.exit(1)
"
'

# 4. Send a test message to admin chat
echo ""
echo "--- Sending test ping to OMNI_TELEGRAM_ADMIN_CHAT_ID ---"
kubectl -n "$NAMESPACE" exec "$POD" -- sh -c '
  token=${TELEGRAM_BOT_TOKEN:-}
  chat_id=${OMNI_TELEGRAM_ADMIN_CHAT_ID:-${TELEGRAM_CHAT_ID:-}}
  if [[ -z "$chat_id" ]]; then
    echo "SKIP: No admin chat_id configured (set OMNI_TELEGRAM_ADMIN_CHAT_ID)"
    exit 0
  fi
  resp=$(curl -s --max-time 10 \
    -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\": $chat_id, \"text\": \"[omni-worker connectivity check] bot is reachable from pod $(hostname)\"}")
  echo "$resp" | python3 -c "
import sys, json
r = json.load(sys.stdin)
if r.get(\"ok\"):
    print(f\"OK: message_id={r[\"result\"][\"message_id\"]}\")
else:
    print(f\"FAIL: {r}\")
    sys.exit(1)
"
'

echo ""
echo "=== Connectivity check complete ==="
