#!/usr/bin/env bash
# Master Plan V3 — idempotent topic create (explicit for lab hygiene).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUBECTL=("${ROOT}/scripts/with_working_kube.sh")

NS="${KAFKA_NAMESPACE:-multi-agent}"
DEPLOY="${KAFKA_DEPLOY:-kafka}"
BOOTSTRAP="${KAFKA_BOOTSTRAP:-kafka:9092}"
TOPICS=(
  "omni-alerts"
  "omni-diagnostic-evidence"
  "omni-actions"
  "omni-action-feedback"
  "omni-dlq"
  "omni-proactive-incidents"
  "omni-audit-sandbox"
  "omni-audit-proactive"
  "omni-audit-agent"
  "omni-tool-audit"
  "omni-hitl-pending"
)

for t in "${TOPICS[@]}"; do
  echo "Ensuring topic: $t"
  "${KUBECTL[@]}" exec -n "$NS" "deploy/$DEPLOY" -- \
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
    --create --if-not-exists --topic "$t" --partitions 1 --replication-factor 1
done
echo "Done."
