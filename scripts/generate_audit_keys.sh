#!/usr/bin/env bash
# Generate Ed25519 keypair for CRAT audit signing.
# Outputs: audit_private_key.pem, audit_public_key.pem + K8s Secret YAML.
set -euo pipefail

OUT_DIR="${1:-/tmp/omni-audit-keys}"
mkdir -p "$OUT_DIR"
PRIV_KEY="$OUT_DIR/audit_private_key.pem"
PUB_KEY="$OUT_DIR/audit_public_key.pem"
SECRET_YAML="$OUT_DIR/omni-audit-keys-secret.yaml"

echo "Generating Ed25519 keypair..."
openssl genpkey -algorithm ed25519 -out "$PRIV_KEY"
openssl pkey -in "$PRIV_KEY" -pubout -out "$PUB_KEY"
chmod 600 "$PRIV_KEY"

echo "Keys written to:"
echo "  Private: $PRIV_KEY"
echo "  Public:  $PUB_KEY"

# Encode for K8s Secret
PRIV_B64=$(base64 -i "$PRIV_KEY" | tr -d '\n')
PUB_B64=$(base64 -i "$PUB_KEY" | tr -d '\n')

cat > "$SECRET_YAML" <<EOF
# K8s Secret for CRAT audit signing keys.
# Apply: kubectl apply -f omni-audit-keys-secret.yaml -n multi-agent
# Mount in pod: OMNI_AUDIT_PRIVATE_KEY_PATH=/etc/omni/audit/private_key.pem
apiVersion: v1
kind: Secret
metadata:
  name: omni-audit-keys
  namespace: multi-agent
type: Opaque
data:
  private_key.pem: $PRIV_B64
  public_key.pem:  $PUB_B64
EOF

echo "K8s Secret YAML: $SECRET_YAML"
echo ""
echo "Next steps:"
echo "  1. kubectl apply -f $SECRET_YAML -n multi-agent"
echo "  2. Mount in omni-worker Deployment:"
echo "       volumeMounts:"
echo "         - name: audit-keys"
echo "           mountPath: /etc/omni/audit"
echo "           readOnly: true"
echo "       env:"
echo "         - name: OMNI_AUDIT_PRIVATE_KEY_PATH"
echo "           value: /etc/omni/audit/private_key.pem"
echo "       volumes:"
echo "         - name: audit-keys"
echo "           secret:"
echo "             secretName: omni-audit-keys"
echo "             defaultMode: 0400"
