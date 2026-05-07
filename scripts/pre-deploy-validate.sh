#!/usr/bin/env bash
# Pre-deployment validation gate — exits non-zero if any prerequisite is missing.
# Run before every production deploy: scripts/pre-deploy-validate.sh
set -euo pipefail

NS="${NS:-multi-agent}"
KAFKA_NS="${KAFKA_NAMESPACE:-multi-agent}"
KAFKA_DEPLOY="${KAFKA_DEPLOY:-kafka}"
BOOTSTRAP="${KAFKA_BOOTSTRAP:-kafka:9092}"
KUBE="./scripts/with_working_kube.sh"

PASS=0
FAIL=0

ok()   { echo "  [OK]  $*"; ((PASS++)) || true; }
fail() { echo "  [FAIL] $*"; ((FAIL++)) || true; }
hdr()  { echo ""; echo "=== $* ==="; }

# ── 1. Kafka broker reachable ─────────────────────────────────────────────────
hdr "Kafka"
if $KUBE exec -n "$KAFKA_NS" "deploy/$KAFKA_DEPLOY" -- \
     /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" --list &>/dev/null; then
  ok "Kafka broker reachable at $BOOTSTRAP"
else
  fail "Kafka broker NOT reachable at $BOOTSTRAP"
fi

# ── 2. Required Kafka topics exist ───────────────────────────────────────────
REQUIRED_TOPICS=(
  "omni-alerts"
  "omni-diagnostic-evidence"
  "omni-actions"
  "omni-action-feedback"
  "omni-audit-chain"
  "omni-hitl-pending"
  "omni-dlq"
)
EXISTING_TOPICS=$($KUBE exec -n "$KAFKA_NS" "deploy/$KAFKA_DEPLOY" -- \
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" --list 2>/dev/null || true)

for topic in "${REQUIRED_TOPICS[@]}"; do
  if echo "$EXISTING_TOPICS" | grep -qx "$topic"; then
    ok "Topic exists: $topic"
  else
    fail "Topic MISSING: $topic — run: make ensure-kafka-topics"
  fi
done

# ── 2b. Validate omni-audit-chain compaction + retention policy ───────────────
hdr "Kafka omni-audit-chain config (CRAT compliance)"
CHAIN_CONFIG=$($KUBE exec -n "$KAFKA_NS" "deploy/$KAFKA_DEPLOY" -- \
  /opt/kafka/bin/kafka-configs.sh --bootstrap-server "$BOOTSTRAP" \
  --describe --entity-type topics --entity-name omni-audit-chain 2>/dev/null || true)

if echo "$CHAIN_CONFIG" | grep -q "cleanup.policy=compact"; then
  ok "omni-audit-chain cleanup.policy=compact"
else
  fail "omni-audit-chain missing cleanup.policy=compact — CRAT compaction broken, run: make ensure-kafka-topics"
fi

if echo "$CHAIN_CONFIG" | grep -q "retention.ms=-1"; then
  ok "omni-audit-chain retention.ms=-1 (infinite — SOX/PCI-DSS compliant)"
else
  fail "omni-audit-chain retention.ms not -1 — CRAT long-term retention at risk, run: make ensure-kafka-topics"
fi

# ── 3. Redis reachable ────────────────────────────────────────────────────────
hdr "Redis"
REDIS_URL="${OMNI_REDIS_URL:-redis://redis:6379/0}"
if $KUBE exec -n "$NS" deploy/omni-core -- \
     python3 -c "import redis.asyncio as r, asyncio; asyncio.run(r.from_url('$REDIS_URL').ping())" &>/dev/null 2>&1; then
  ok "Redis reachable"
else
  # Fallback: kubectl exec into a running pod with redis-cli
  if $KUBE exec -n "$NS" deploy/omni-core -- redis-cli -u "$REDIS_URL" ping 2>/dev/null | grep -q PONG; then
    ok "Redis reachable (redis-cli)"
  else
    fail "Redis NOT reachable at $REDIS_URL"
  fi
fi

# ── 4. Required K8s Secrets exist ────────────────────────────────────────────
hdr "K8s Secrets ($NS)"
REQUIRED_SECRETS=(
  "telegram-bot"
  "omni-audit-keys"
)
for secret in "${REQUIRED_SECRETS[@]}"; do
  if $KUBE get secret "$secret" -n "$NS" &>/dev/null; then
    ok "Secret exists: $secret"
  else
    fail "Secret MISSING: $secret"
  fi
done

# Dashboard auth secret (optional — only if UI is deployed)
if $KUBE get deployment omni-dashboard -n "$NS" &>/dev/null 2>&1; then
  if $KUBE get secret omni-dashboard-auth -n "$NS" &>/dev/null; then
    ok "Secret exists: omni-dashboard-auth"
  else
    fail "Secret MISSING: omni-dashboard-auth (required for UI)"
  fi
fi

# ── 5. ServiceAccount and RBAC ────────────────────────────────────────────────
hdr "RBAC ($NS)"
for sa in "omni-worker" "omni-executor"; do
  if $KUBE get serviceaccount "$sa" -n "$NS" &>/dev/null; then
    ok "ServiceAccount exists: $sa"
  else
    fail "ServiceAccount MISSING: $sa"
  fi
done

for rb in "omni-worker-binding" "omni-executor-binding"; do
  if $KUBE get rolebinding "$rb" -n "$NS" &>/dev/null; then
    ok "RoleBinding exists: $rb"
  else
    fail "RoleBinding MISSING: $rb — run: make deploy-worker"
  fi
done

# ── 6. ConfigMap ──────────────────────────────────────────────────────────────
hdr "ConfigMap ($NS)"
if $KUBE get configmap omni-worker-config -n "$NS" &>/dev/null; then
  ok "ConfigMap exists: omni-worker-config"
else
  fail "ConfigMap MISSING: omni-worker-config — run: make deploy-worker"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=============================="
echo "  PASS: $PASS   FAIL: $FAIL"
echo "=============================="
if [[ $FAIL -gt 0 ]]; then
  echo "Pre-deploy validation FAILED — fix the above before deploying."
  exit 1
fi
echo "Pre-deploy validation PASSED."
