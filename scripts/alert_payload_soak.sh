#!/usr/bin/env bash
# Gửi lặp cùng payload tới gateway (Giai đoạn 3 — soak tùy chọn).
# Usage: NS=<ns> REPEAT=30 INTERVAL_SEC=2 ./scripts/alert_payload_soak.sh scripts/alert_payloads/replay/replay_example_minimal.json
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -z "${NS:-}" ]]; then
  echo "alert_payload_soak.sh: set NS (passed to post_gateway_alert.sh)." >&2
  exit 2
fi
export NS
RAW="${1:?usage: $0 path/to/payload.json}"
REPEAT="${REPEAT:-20}"
INTERVAL_SEC="${INTERVAL_SEC:-3}"

if [[ -f "$RAW" ]]; then
  ABS="$(cd "$(dirname "$RAW")" && pwd)/$(basename "$RAW")"
elif [[ -f "$ROOT/$RAW" ]]; then
  ABS="$ROOT/$RAW"
else
  echo "Missing payload: $RAW" >&2
  exit 1
fi
PAYLOAD="$ABS"

echo "soak: repeat=$REPEAT interval=${INTERVAL_SEC}s payload=$PAYLOAD"
for ((i=1; i<=REPEAT; i++)); do
  echo "--- $i/$REPEAT ---"
  bash "$ROOT/scripts/alert_flow_realistic/post_gateway_alert.sh" "$PAYLOAD" || true
  sleep "$INTERVAL_SEC"
done
echo "done — kiểm tra Kafka lag / duplicate mutate thủ công"
