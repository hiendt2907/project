#!/usr/bin/env bash
# verify_hitl_production.sh — Production readiness gate for the HITL + SIEM stack.
#
# Checks:
#   1. Deployments Running: omni-hitl-dispatcher, omni-siem-bridge, omni-evidence-adapter (multi-agent)
#                           finguard-hitl-api (finguard-customer)
#   2. PodDisruptionBudget for omni-hitl-dispatcher exists
#   3. NetworkPolicy for omni-hitl-dispatcher-netpol exists (multi-agent)
#      NetworkPolicy for allow-hitl-api-from-omni exists (finguard-customer)
#   4. Kafka topic omni-hitl-pending exists
#   5. From a running dispatcher pod:
#        a. DNS resolves finguard-hitl-api.finguard-customer.svc.cluster.local
#        b. GET /healthz == 200
#        c. GET /v1/hitl/decisions without token == 401
#        d. GET /v1/hitl/decisions with token == 200 or 404 (not 401/403)
#
# Exit 0 = GO, non-zero = NO-GO (blockers listed on stderr).
#
set -euo pipefail

PASS=0
FAIL=0
BLOCKERS=()

MULTI_NS="multi-agent"
FINGUARD_NS="finguard-customer"
HITL_API_HOST="finguard-hitl-api.finguard-customer.svc.cluster.local"
HITL_API_PORT="8081"

ok()  { echo "  [OK]  $*"; ((PASS++)) || true; }
fail(){ echo "  [FAIL] $*" >&2; ((FAIL++)) || true; BLOCKERS+=("$*"); }

# ── Helper: check deployment is ready ───────────────────────────────────────
check_deployment() {
  local ns="$1" name="$2"
  local ready
  ready=$(kubectl get deployment "$name" -n "$ns" \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
  local desired
  desired=$(kubectl get deployment "$name" -n "$ns" \
    -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "1")
  if [[ "$ready" == "$desired" && "$ready" != "0" ]]; then
    ok "Deployment $ns/$name: ${ready}/${desired} ready"
  else
    fail "Deployment $ns/$name: ${ready:-0}/${desired} ready — not Running"
  fi
}

# ── Helper: check resource exists ───────────────────────────────────────────
check_resource() {
  local kind="$1" ns="$2" name="$3"
  if kubectl get "$kind" "$name" -n "$ns" &>/dev/null; then
    ok "$kind $ns/$name exists"
  else
    fail "$kind $ns/$name missing"
  fi
}

echo "========================================================"
echo " HITL Production Readiness Gate"
echo "========================================================"

# ── 1. Deployment readiness ──────────────────────────────────────────────────
echo
echo "── 1. Deployment health ──"
check_deployment "$MULTI_NS"   "omni-hitl-dispatcher"
check_deployment "$MULTI_NS"   "omni-siem-bridge"
check_deployment "$MULTI_NS"   "omni-evidence-adapter"
check_deployment "$FINGUARD_NS" "finguard-hitl-api"

# ── 2. PodDisruptionBudget ───────────────────────────────────────────────────
echo
echo "── 2. PodDisruptionBudget ──"
check_resource "poddisruptionbudget" "$MULTI_NS" "omni-hitl-dispatcher-pdb"

# ── 3. NetworkPolicies ───────────────────────────────────────────────────────
echo
echo "── 3. NetworkPolicies ──"
check_resource "networkpolicy" "$MULTI_NS"    "omni-hitl-dispatcher-netpol"
check_resource "networkpolicy" "$FINGUARD_NS" "allow-hitl-api-from-omni"

# ── 4. Kafka topic ───────────────────────────────────────────────────────────
echo
echo "── 4. Kafka topic omni-hitl-pending ──"
KAFKA_POD=$(kubectl get pod -n "$MULTI_NS" -l app=kafka \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [[ -z "$KAFKA_POD" ]]; then
  fail "No Kafka pod found in ns $MULTI_NS — cannot verify topic"
else
  TOPIC_MATCH=$(kubectl exec "$KAFKA_POD" -n "$MULTI_NS" -- \
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list 2>/dev/null \
    | grep "^omni-hitl-pending$" | wc -l | tr -d ' ' || echo "0")
  if [[ "${TOPIC_MATCH:-0}" -ge 1 ]]; then
    ok "Kafka topic omni-hitl-pending exists"
  else
    fail "Kafka topic omni-hitl-pending NOT found"
  fi
fi

# ── 5. In-pod connectivity checks ────────────────────────────────────────────
echo
echo "── 5. In-pod connectivity from omni-hitl-dispatcher ──"
DISPATCHER_POD=$(kubectl get pod -n "$MULTI_NS" -l app=omni-hitl-dispatcher \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

if [[ -z "$DISPATCHER_POD" ]]; then
  fail "No Running omni-hitl-dispatcher pod — skipping in-pod checks"
else
  echo "  Using pod: $DISPATCHER_POD"

  # 5a. DNS resolution
  DNS_OK=$(kubectl exec "$DISPATCHER_POD" -n "$MULTI_NS" -- \
    python3 -c "import socket; socket.getaddrinfo('${HITL_API_HOST}', ${HITL_API_PORT}); print('ok')" \
    2>/dev/null || echo "")
  if [[ "$DNS_OK" == "ok" ]]; then
    ok "DNS resolves ${HITL_API_HOST}"
  else
    fail "DNS resolution failed for ${HITL_API_HOST}"
  fi

  # 5b. GET /healthz == 200
  HEALTHZ_CODE=$(kubectl exec "$DISPATCHER_POD" -n "$MULTI_NS" -- \
    python3 -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('http://${HITL_API_HOST}:${HITL_API_PORT}/healthz', timeout=5)
    print(r.status)
except Exception as e:
    print('ERR:' + str(e))
" 2>/dev/null || echo "ERR:exec_failed")
  if [[ "$HEALTHZ_CODE" == "200" ]]; then
    ok "GET /healthz => 200"
  else
    fail "GET /healthz => ${HEALTHZ_CODE} (expected 200)"
  fi

  # 5c. GET /v1/hitl/pending without token == 401
  UNAUTH_CODE=$(kubectl exec "$DISPATCHER_POD" -n "$MULTI_NS" -- \
    python3 -c "
import urllib.request, urllib.error, sys
req = urllib.request.Request('http://${HITL_API_HOST}:${HITL_API_PORT}/v1/hitl/pending')
try:
    urllib.request.urlopen(req, timeout=5)
    print('200')
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print('ERR:' + str(e))
" 2>/dev/null || echo "ERR:exec_failed")
  if [[ "$UNAUTH_CODE" == "401" ]]; then
    ok "GET /v1/hitl/pending (no token) => 401"
  else
    fail "GET /v1/hitl/pending (no token) => ${UNAUTH_CODE} (expected 401)"
  fi

  # 5d. GET /v1/hitl/pending with token == 200 (auth accepted; empty list is fine)
  # Note: /v1/hitl/decisions is POST-only; /v1/hitl/pending is the GET listing endpoint.
  TOKEN_FROM_SECRET=$(kubectl get secret hitl-dispatcher-secret -n "$MULTI_NS" \
    -o jsonpath='{.data.hitl_api_token}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
  if [[ -z "$TOKEN_FROM_SECRET" ]]; then
    fail "Cannot read hitl-dispatcher-secret in $MULTI_NS — skipping auth check"
  else
    AUTH_CODE=$(kubectl exec "$DISPATCHER_POD" -n "$MULTI_NS" -- \
      python3 -c "
import urllib.request, urllib.error, sys
req = urllib.request.Request('http://${HITL_API_HOST}:${HITL_API_PORT}/v1/hitl/pending')
req.add_header('Authorization', 'Bearer ${TOKEN_FROM_SECRET}')
try:
    r = urllib.request.urlopen(req, timeout=5)
    print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print('ERR:' + str(e))
" 2>/dev/null || echo "ERR:exec_failed")
    if [[ "$AUTH_CODE" == "200" || "$AUTH_CODE" == "404" ]]; then
      ok "GET /v1/hitl/pending (with token) => ${AUTH_CODE} (auth accepted)"
    else
      fail "GET /v1/hitl/pending (with token) => ${AUTH_CODE} (expected 200 or 404)"
    fi
  fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo
echo "========================================================"
echo " Results: ${PASS} passed, ${FAIL} failed"
echo "========================================================"

if [[ ${FAIL} -gt 0 ]]; then
  echo
  echo "NO-GO — Blockers:"
  for b in "${BLOCKERS[@]}"; do
    echo "  • $b"
  done
  echo
  echo "Rollback commands:"
  echo "  kubectl rollout undo deployment/omni-hitl-dispatcher -n $MULTI_NS"
  echo "  kubectl rollout undo deployment/finguard-hitl-api -n $FINGUARD_NS"
  exit 1
fi

echo
echo "GO — All checks passed. HITL stack is production-ready."
