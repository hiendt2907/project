#!/usr/bin/env bash
# One-time Vault bootstrap for the GCP single-node cluster: init (Shamir
# 1-of-1 — this is a single-node lab/production box, not a real multi-key HA
# quorum; raise key-shares/threshold if Vault ever runs on more than one
# node), unseal, enable KV v2 + Kubernetes auth, and the policy/role that
# lets External Secrets Operator read secrets. Idempotent: safe to re-run
# from Jenkins on every build, skips init if Vault is already initialized.
#
# The unseal key + root token are generated once and stored as a k8s Secret
# (vault-unseal-bootstrap, namespace vault) so unattended CI re-runs can
# re-unseal after a pod restart without a human re-typing them. This is the
# same trust boundary already used for every other admin credential in this
# repo (grafana-admin, harbor-admin-bootstrap) — real production should swap
# this for GCP KMS auto-unseal instead of a Shamir key at all.
set -euo pipefail

NS=vault
POD=vault-0
BOOTSTRAP_SECRET=vault-unseal-bootstrap

kubectl get namespace "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"

STATUS=$(kubectl exec -n "$NS" "$POD" -- vault status -format=json 2>/dev/null || true)
INITIALIZED=$(echo "$STATUS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('initialized', False))" 2>/dev/null || echo "false")

if [ "$INITIALIZED" != "True" ]; then
  echo "=== Initializing Vault (1-of-1 Shamir key — single-node lab) ==="
  INIT_JSON=$(kubectl exec -n "$NS" "$POD" -- vault operator init -key-shares=1 -key-threshold=1 -format=json)
  UNSEAL_KEY=$(echo "$INIT_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['unseal_keys_b64'][0])")
  ROOT_TOKEN=$(echo "$INIT_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['root_token'])")
  kubectl create secret generic "$BOOTSTRAP_SECRET" -n "$NS" \
    --from-literal=unseal_key="$UNSEAL_KEY" \
    --from-literal=root_token="$ROOT_TOKEN"
else
  UNSEAL_KEY=$(kubectl get secret "$BOOTSTRAP_SECRET" -n "$NS" -o jsonpath="{.data.unseal_key}" | base64 -d)
  ROOT_TOKEN=$(kubectl get secret "$BOOTSTRAP_SECRET" -n "$NS" -o jsonpath="{.data.root_token}" | base64 -d)
fi

SEALED=$(kubectl exec -n "$NS" "$POD" -- vault status -format=json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['sealed'])")
if [ "$SEALED" = "True" ]; then
  echo "=== Unsealing Vault ==="
  kubectl exec -n "$NS" "$POD" -- vault operator unseal "$UNSEAL_KEY"
fi

echo "=== Configuring KV v2 + Kubernetes auth + external-secrets policy/role ==="
kubectl exec -n "$NS" "$POD" -- sh -c "
export VAULT_TOKEN=$ROOT_TOKEN
vault secrets enable -path=secret kv-v2 2>&1 | grep -v 'path is already in use' || true
vault auth enable kubernetes 2>&1 | grep -v 'path is already in use' || true
vault write auth/kubernetes/config kubernetes_host=https://\$KUBERNETES_SERVICE_HOST:\$KUBERNETES_SERVICE_PORT
cat > /tmp/eso-policy.hcl << 'EOP'
path \"secret/data/*\" {
  capabilities = [\"read\", \"list\"]
}
EOP
vault policy write external-secrets /tmp/eso-policy.hcl
vault write auth/kubernetes/role/external-secrets \
  bound_service_account_names=external-secrets \
  bound_service_account_namespaces=external-secrets \
  policies=external-secrets \
  ttl=1h
rm -f /tmp/eso-policy.hcl
"
echo "=== Done ==="
