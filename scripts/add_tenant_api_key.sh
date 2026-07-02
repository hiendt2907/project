#!/usr/bin/env bash
# Idempotently add a tenant's API key to omni-gateway-secret's OMNI_TENANT_APIKEYS entry.
# Canonicalizes a step that was previously done by hand via kubectl (see iteration 9,
# docs/product/PRODUCT_PROOF.md).
#
# Usage: bash scripts/add_tenant_api_key.sh <tenant_id> [api_key]
#   - If api_key is omitted, a new one is generated (openssl rand -hex 32).
#   - If tenant_id is already present, the script is a no-op (prints existing key, exits 0).
#
# After success: rolling-restarts omni-gateway so the new key takes effect immediately.
set -euo pipefail

NS="${NS:-multi-agent}"
KUBE="./scripts/with_working_kube.sh"
SECRET_NAME="omni-gateway-secret"
FIELD="OMNI_TENANT_APIKEYS"

TENANT_ID="${1:?Usage: add_tenant_api_key.sh <tenant_id> [api_key]}"
API_KEY="${2:-}"

CURRENT=$($KUBE get secret "$SECRET_NAME" -n "$NS" -o jsonpath="{.data.$FIELD}" | base64 -d)

# Idempotent: bail out early if tenant_id already has an entry.
IFS=',' read -ra PAIRS <<< "$CURRENT"
for pair in "${PAIRS[@]}"; do
  pair_tid="${pair%%:*}"
  if [[ "$pair_tid" == "$TENANT_ID" ]]; then
    echo "[add_tenant_api_key] tenant '$TENANT_ID' already present — no-op."
    echo "[add_tenant_api_key] existing key: ${pair#*:}"
    exit 0
  fi
done

if [[ -z "$API_KEY" ]]; then
  API_KEY=$(openssl rand -hex 32)
  echo "[add_tenant_api_key] generated new key for '$TENANT_ID'"
fi

NEW_VALUE="${CURRENT},${TENANT_ID}:${API_KEY}"

$KUBE patch secret "$SECRET_NAME" -n "$NS" \
  --type=json \
  -p="[{\"op\": \"replace\", \"path\": \"/data/${FIELD}\", \"value\": \"$(printf '%s' "$NEW_VALUE" | base64)\"}]"

echo "[add_tenant_api_key] secret updated. Rolling restart gateway..."
$KUBE rollout restart deployment/omni-gateway -n "$NS"
$KUBE rollout status deployment/omni-gateway -n "$NS" --timeout=120s

echo ""
echo "[add_tenant_api_key] DONE."
echo "  tenant_id: $TENANT_ID"
echo "  api_key:   $API_KEY"
