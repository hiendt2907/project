#!/usr/bin/env bash
# Debug + verify the full advisory → Telegram notification pipeline.
# 1. Confirms credentials and network connectivity
# 2. Injects a synthetic advisory message directly via the emitter logic
# 3. Reports Kafka consumer offset for the advisory emitter
# Usage: ./scripts/debug_telegram_flow.sh [namespace]
set -euo pipefail

NAMESPACE="${1:-multi-agent}"
POD=$(kubectl -n "$NAMESPACE" get pod -l app=omni-worker -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

if [[ -z "$POD" ]]; then
  echo "ERROR: No omni-worker pod found in namespace $NAMESPACE"
  exit 1
fi

echo "╔══════════════════════════════════════════════════════╗"
echo "║  Omni Advisory → Telegram Debug Flow                ║"
echo "║  pod: $POD  ns: $NAMESPACE"
echo "╚══════════════════════════════════════════════════════╝"

# ── Step 1: Credentials & network ────────────────────────────────────────────
echo ""
echo "── Step 1: Credentials & network ──"
"$(dirname "$0")/check_connectivity.sh" "$NAMESPACE" || {
  echo "FAIL: Connectivity check failed — fix credentials/network before proceeding."
  exit 1
}

# ── Step 2: Inject synthetic advisory message ────────────────────────────────
echo ""
echo "── Step 2: Send synthetic advisory message ──"
kubectl -n "$NAMESPACE" exec "$POD" -- python3 - <<'PYEOF'
import asyncio, os, sys
sys.path.insert(0, "/app/src")

from ingest.telegram import TelegramClient, TelegramBotSettings
from workers.settings import WorkerSettings

async def main():
    settings = WorkerSettings()
    chat_id = settings.telegram_admin_chat_id or int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
    if not chat_id:
        print("SKIP: no chat_id (OMNI_TELEGRAM_ADMIN_CHAT_ID / TELEGRAM_CHAT_ID unset)")
        return

    client = TelegramClient.from_settings(TelegramBotSettings())
    msg = (
        "🔬 *Advisory Debug Heartbeat*\n"
        "Source: `debug\_telegram\_flow.sh`\n"
        "Status: pipeline wiring verified ✅\n"
        "If you see this message the Telegram notification path is working."
    )
    try:
        r = await client.send_message(chat_id, msg, parse_mode="Markdown")
        print(f"OK: message_id={r['result']['message_id']} chat_id={chat_id}")
    except Exception as e:
        print(f"FAIL: {e!r}")
        sys.exit(1)
    finally:
        await client.aclose()

asyncio.run(main())
PYEOF

# ── Step 3: Consumer offset for advisory Kafka topics ───────────────────────
echo ""
echo "── Step 3: Kafka consumer offsets ──"
KAFKA_POD=$(kubectl -n "$NAMESPACE" get pod -l app=kafka -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [[ -z "$KAFKA_POD" ]]; then
  echo "SKIP: no kafka pod found in $NAMESPACE"
else
  echo "omni-diagnostic-evidence consumer offsets:"
  kubectl -n "$NAMESPACE" exec "$KAFKA_POD" -- \
    kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
    --describe --group omni-worker 2>/dev/null | grep -E "omni-diagnostic-evidence|TOPIC|LAG" || echo "(none)"

  echo ""
  echo "omni-action-feedback consumer offsets:"
  kubectl -n "$NAMESPACE" exec "$KAFKA_POD" -- \
    kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
    --describe --group omni-worker 2>/dev/null | grep -E "omni-action-feedback|TOPIC|LAG" || echo "(none)"
fi

# ── Step 4: Recent advisory log events ──────────────────────────────────────
echo ""
echo "── Step 4: Recent advisory_telegram log events (last 100 lines) ──"
kubectl -n "$NAMESPACE" logs "$POD" --tail=100 2>/dev/null \
  | grep -E "advisory_telegram|advisory_analyst|advisory_no_chat_id|telegram_send" \
  || echo "(no matching log lines)"

echo ""
echo "═══ Debug complete. Check Step 2 output for confirmation. ═══"
