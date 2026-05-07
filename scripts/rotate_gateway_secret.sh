#!/usr/bin/env bash
# Rotate Alertmanager webhook HMAC secret — zero-downtime gateway rolling restart.
# Usage: bash scripts/rotate_gateway_secret.sh [--dry-run]
# After rotation: update Alertmanager config with new OMNI_GATEWAY_WEBHOOK_SECRET.
set -euo pipefail

NS="${NS:-multi-agent}"
KUBE="./scripts/with_working_kube.sh"
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# Generate cryptographically random secret (32 bytes = 64 hex chars)
NEW_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
echo "[rotate_gateway_secret] New secret generated (first 8 chars): ${NEW_SECRET:0:8}..."

if $DRY_RUN; then
  echo "[DRY-RUN] Would update omni-gateway-secret in namespace $NS"
  echo "[DRY-RUN] Would restart: omni-gateway"
  echo "[DRY-RUN] Update Alertmanager webhook URL secret after rotation."
  exit 0
fi

# Apply new K8s secret
$KUBE apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: omni-gateway-secret
  namespace: $NS
  annotations:
    omni.io/rotated-at: "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
type: Opaque
stringData:
  OMNI_GATEWAY_WEBHOOK_SECRET: "$NEW_SECRET"
EOF

echo "[rotate_gateway_secret] Secret updated. Rolling restart gateway..."
$KUBE rollout restart deployment/omni-gateway -n "$NS"
$KUBE rollout status deployment/omni-gateway -n "$NS" --timeout=120s

echo ""
echo "[rotate_gateway_secret] DONE. Next steps:"
echo "  1. Update Alertmanager webhook configuration with the new secret:"
echo "     OMNI_GATEWAY_WEBHOOK_SECRET=${NEW_SECRET}"
echo "  2. Reload Alertmanager: kubectl rollout restart deployment/alertmanager -n monitor"
echo "  3. Verify: curl -X POST https://gateway.ai-agent.local/webhook/prometheus with new HMAC"
echo ""
echo "WARNING: Old secret is now INVALID. Update Alertmanager BEFORE testing."
