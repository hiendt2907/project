#!/usr/bin/env bash
# Copy telegram-bot (multi-agent) -> grafana-telegram-alerting (monitor).
# Same keys as omni-worker: bot-token, chat-id. Run after ./scripts/with_working_kube.sh is usable.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WRAPPER="${ROOT}/scripts/with_working_kube.sh"
SRC_NS="${SRC_NS:-multi-agent}"
DST_NS="${DST_NS:-monitor}"
SRC_NAME="${SRC_NAME:-telegram-bot}"
DST_NAME="${DST_NAME:-grafana-telegram-alerting}"

BOT=$("$WRAPPER" get secret "$SRC_NAME" -n "$SRC_NS" -o jsonpath='{.data.bot-token}' | base64 -d)
CHAT=$("$WRAPPER" get secret "$SRC_NAME" -n "$SRC_NS" -o jsonpath='{.data.chat-id}' | base64 -d)

"$WRAPPER" create secret generic "$DST_NAME" -n "$DST_NS" \
  --from-literal=bot-token="$BOT" \
  --from-literal=chat-id="$CHAT" \
  --dry-run=client -o yaml | "$WRAPPER" apply -f -

echo "Synced $SRC_NAME@$SRC_NS -> $DST_NAME@$DST_NS. Restart Grafana: $WRAPPER rollout restart deployment/grafana -n $DST_NS"
