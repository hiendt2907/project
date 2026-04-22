#!/usr/bin/env bash
# rotate_hitl_token.sh — Sync the HITL shared bearer token to both namespaces.
#
# The omni-hitl-dispatcher (multi-agent) and finguard-hitl-api (finguard-customer)
# MUST share the same token value. This script creates or replaces both secrets
# atomically and verifies SHA-256 hash parity before finishing.
#
# Usage:
#   HITL_TOKEN=<new_token> bash scripts/rotate_hitl_token.sh
#   or:
#   bash scripts/rotate_hitl_token.sh   # prompts for token interactively
#
# Guardrails:
#   - Fails closed if token looks like a placeholder (CHANGEME / <TOKEN> / empty)
#   - Prints only token length + first 16 hex chars of sha256 — never the full value
#   - Verifies both namespaces have identical SHA-256 before exiting
#
set -euo pipefail

MULTI_AGENT_NS="multi-agent"
FINGUARD_NS="finguard-customer"
SECRET_NAME_DISPATCHER="hitl-dispatcher-secret"
SECRET_KEY_DISPATCHER="hitl_api_token"
SECRET_NAME_HITL_API="hitl-api-token"
SECRET_KEY_HITL_API="hitl_api_token"

# ── 1. Acquire token ─────────────────────────────────────────────────────────
if [[ -z "${HITL_TOKEN:-}" ]]; then
  echo "[rotate_hitl_token] No HITL_TOKEN env var. Reading from stdin (hidden):"
  read -r -s HITL_TOKEN
  echo
fi

# ── 2. Fail-closed: reject placeholder values ────────────────────────────────
if [[ -z "$HITL_TOKEN" ]]; then
  echo "[rotate_hitl_token] FATAL: empty token. Aborting." >&2
  exit 2
fi

lower=$(printf '%s' "$HITL_TOKEN" | tr '[:upper:]' '[:lower:]')
for bad in "changeme" "<token>" "placeholder" "todo" "fixme" "example" "testtoken" "secret"; do
  if [[ "$lower" == *"$bad"* ]]; then
    echo "[rotate_hitl_token] FATAL: token contains placeholder pattern '$bad'. Aborting." >&2
    exit 2
  fi
done

if [[ ${#HITL_TOKEN} -lt 16 ]]; then
  echo "[rotate_hitl_token] FATAL: token is too short (${#HITL_TOKEN} chars, minimum 16). Aborting." >&2
  exit 2
fi

TOKEN_HASH=$(printf '%s' "$HITL_TOKEN" | sha256sum | awk '{print $1}')
TOKEN_HASH_SHORT="${TOKEN_HASH:0:16}"
echo "[rotate_hitl_token] Token accepted: length=${#HITL_TOKEN} sha256[:16]=${TOKEN_HASH_SHORT}"

# ── 3. Apply to multi-agent namespace (dispatcher) ──────────────────────────
echo "[rotate_hitl_token] Updating secret '${SECRET_NAME_DISPATCHER}' in ns '${MULTI_AGENT_NS}'..."
kubectl create secret generic "$SECRET_NAME_DISPATCHER" \
  --from-literal="${SECRET_KEY_DISPATCHER}=${HITL_TOKEN}" \
  --namespace="$MULTI_AGENT_NS" \
  --dry-run=client -o yaml | kubectl apply -f -

# ── 4. Apply to finguard-customer namespace (hitl-api) ───────────────────────
echo "[rotate_hitl_token] Updating secret '${SECRET_NAME_HITL_API}' in ns '${FINGUARD_NS}'..."
kubectl create secret generic "$SECRET_NAME_HITL_API" \
  --from-literal="${SECRET_KEY_HITL_API}=${HITL_TOKEN}" \
  --namespace="$FINGUARD_NS" \
  --dry-run=client -o yaml | kubectl apply -f -

# ── 5. Hash parity verification ──────────────────────────────────────────────
echo "[rotate_hitl_token] Verifying hash parity across namespaces..."

fetch_hash() {
  local ns="$1" secret="$2" key="$3"
  kubectl get secret "$secret" -n "$ns" -o jsonpath="{.data.${key}}" 2>/dev/null \
    | base64 -d \
    | sha256sum | awk '{print $1}'
}

HASH_DISPATCHER=$(fetch_hash "$MULTI_AGENT_NS" "$SECRET_NAME_DISPATCHER" "$SECRET_KEY_DISPATCHER")
HASH_HITL_API=$(fetch_hash "$FINGUARD_NS" "$SECRET_NAME_HITL_API" "$SECRET_KEY_HITL_API")

if [[ "$HASH_DISPATCHER" != "$HASH_HITL_API" ]]; then
  echo "[rotate_hitl_token] FATAL: hash mismatch after apply!" >&2
  echo "  dispatcher sha256[:16]: ${HASH_DISPATCHER:0:16}" >&2
  echo "  hitl-api   sha256[:16]: ${HASH_HITL_API:0:16}" >&2
  exit 3
fi

if [[ "${HASH_DISPATCHER:0:16}" != "$TOKEN_HASH_SHORT" ]]; then
  echo "[rotate_hitl_token] FATAL: cluster hash does not match supplied token!" >&2
  echo "  expected sha256[:16]: ${TOKEN_HASH_SHORT}" >&2
  echo "  cluster  sha256[:16]: ${HASH_DISPATCHER:0:16}" >&2
  exit 3
fi

echo "[rotate_hitl_token] OK: both namespaces have matching token (sha256[:16]=${TOKEN_HASH_SHORT})"

# ── 6. Trigger rolling restart so pods pick up new secret ───────────────────
echo "[rotate_hitl_token] Restarting omni-hitl-dispatcher to reload token..."
kubectl rollout restart deployment/omni-hitl-dispatcher -n "$MULTI_AGENT_NS"
echo "[rotate_hitl_token] Restarting finguard-hitl-api to reload token..."
kubectl rollout restart deployment/finguard-hitl-api -n "$FINGUARD_NS"

echo "[rotate_hitl_token] Rotation complete. Monitor rollouts with:"
echo "  kubectl rollout status deployment/omni-hitl-dispatcher -n ${MULTI_AGENT_NS}"
echo "  kubectl rollout status deployment/finguard-hitl-api -n ${FINGUARD_NS}"
