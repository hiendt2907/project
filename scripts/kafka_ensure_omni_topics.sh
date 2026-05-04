#!/usr/bin/env bash
# Master Plan V3 — idempotent topic create (explicit for lab hygiene).
#
# Retention / policy (documented; no topic renames or deletes):
# - omni-diagnostic-evidence: broker default at create unless you set
#   OMNI_DIAGNOSTIC_EVIDENCE_RETENTION_MS (e.g. 604800000 = 7d). Applied only
#   when the topic is first created (--create --if-not-exists). To change an
#   existing topic, use kafka-configs.sh --alter (not this script).
# - omni-audit-chain: compact + infinite retention by default (CRAT). Override
#   at create time with OMNI_AUDIT_CHAIN_RETENTION_MS (default -1) and
#   OMNI_AUDIT_CHAIN_MIN_INSYNC_REPLICAS (default 1).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUBECTL=("${ROOT}/scripts/with_working_kube.sh")

NS="${KAFKA_NAMESPACE:-multi-agent}"
DEPLOY="${KAFKA_DEPLOY:-kafka}"
BOOTSTRAP="${KAFKA_BOOTSTRAP:-kafka:9092}"

# CRAT chain topic: keep defaults aligned with comment block above.
OMNI_AUDIT_CHAIN_RETENTION_MS="${OMNI_AUDIT_CHAIN_RETENTION_MS:--1}"
OMNI_AUDIT_CHAIN_MIN_INSYNC_REPLICAS="${OMNI_AUDIT_CHAIN_MIN_INSYNC_REPLICAS:-1}"

TOPICS=(
  "omni-alerts"
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

# Diagnostic evidence lane (see header): optional finite retention at first create.
DIAG_EVIDENCE_EXTRA=()
if [[ -n "${OMNI_DIAGNOSTIC_EVIDENCE_RETENTION_MS:-}" ]]; then
  DIAG_EVIDENCE_EXTRA+=(--config "retention.ms=${OMNI_DIAGNOSTIC_EVIDENCE_RETENTION_MS}")
fi
if ((${#DIAG_EVIDENCE_EXTRA[@]})); then
  echo "Ensuring topic: omni-diagnostic-evidence (retention.ms=${OMNI_DIAGNOSTIC_EVIDENCE_RETENTION_MS})"
else
  echo "Ensuring topic: omni-diagnostic-evidence (no retention.ms at create; broker default — see header)"
fi
"${KUBECTL[@]}" exec -n "$NS" "deploy/$DEPLOY" -- \
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
  --create --if-not-exists --topic "omni-diagnostic-evidence" \
  --partitions 1 --replication-factor 1 \
  "${DIAG_EVIDENCE_EXTRA[@]}"

# CRAT audit chain: compacted + infinite retention (SOX 404, PCI-DSS v4.0).
# cleanup.policy=compact preserves the latest block per key; retention.ms=-1 = keep forever.
echo "Ensuring topic: omni-audit-chain (compact, retention.ms=${OMNI_AUDIT_CHAIN_RETENTION_MS})"
"${KUBECTL[@]}" exec -n "$NS" "deploy/$DEPLOY" -- \
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
  --create --if-not-exists --topic "omni-audit-chain" \
  --partitions 1 --replication-factor 1 \
  --config cleanup.policy=compact \
  --config "retention.ms=${OMNI_AUDIT_CHAIN_RETENTION_MS}" \
  --config "min.insync.replicas=${OMNI_AUDIT_CHAIN_MIN_INSYNC_REPLICAS}"

echo "Done."
