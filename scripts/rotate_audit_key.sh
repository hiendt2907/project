#!/usr/bin/env bash
# Rotate CRAT Ed25519 audit signing key — zero-downtime rolling restart.
# Usage: bash scripts/rotate_audit_key.sh [--dry-run]
set -euo pipefail

NS="${NS:-multi-agent}"
KUBE="./scripts/with_working_kube.sh"
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

OUT_DIR="$(mktemp -d)"
trap 'rm -rf "$OUT_DIR"' EXIT

echo "[rotate_audit_key] Generating new Ed25519 keypair..."
bash scripts/generate_audit_keys.sh "$OUT_DIR"

NEW_PRIV="$OUT_DIR/audit_private_key.pem"
NEW_PUB="$OUT_DIR/audit_public_key.pem"

if [[ ! -f "$NEW_PRIV" ]] || [[ ! -f "$NEW_PUB" ]]; then
  echo "[ERROR] Key generation failed — files not found"
  exit 1
fi

# Annotate new key with rotation timestamp
NEW_VERSION="$(date +%Y%m%d-%H%M%S)"
PRIV_B64=$(base64 -i "$NEW_PRIV" | tr -d '\n')
PUB_B64=$(base64 -i "$NEW_PUB" | tr -d '\n')

echo "[rotate_audit_key] Key version: $NEW_VERSION"

if $DRY_RUN; then
  echo "[DRY-RUN] Would apply new omni-audit-keys secret to namespace $NS"
  echo "[DRY-RUN] Would restart: omni-fullstack"
  exit 0
fi

# Apply new secret
$KUBE apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: omni-audit-keys
  namespace: $NS
  annotations:
    omni.io/key-version: "$NEW_VERSION"
    omni.io/rotated-at: "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
type: Opaque
data:
  private_key.pem: $PRIV_B64
  public_key.pem: $PUB_B64
EOF

echo "[rotate_audit_key] Secret updated. Triggering rolling restart..."
$KUBE rollout restart deployment/omni-fullstack -n "$NS"
$KUBE rollout status deployment/omni-fullstack -n "$NS" --timeout=180s

echo "[rotate_audit_key] Verifying CRAT integrity with new key..."
# Give pods 10s to pick up new key
sleep 10
python3 scripts/crat_integrity_check.py

echo "[rotate_audit_key] Rotation complete. New key version: $NEW_VERSION"
echo "IMPORTANT: Store the new public key for signature verification:"
cat "$NEW_PUB"
