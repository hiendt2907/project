# shellcheck shell=bash
# E2E environment resolver — single source of truth for lab endpoints.
#
# Reality (2026-06-10):
#   - Gateway is reached through Traefik ingress http://gateway.ai-agent.local
#     (Ingress omni-gateway + omni-gateway-sse, /etc/hosts → 192.168.139.2).
#   - Gateway auth is HTTP Bearer (gateway/api.py HTTPBearer) — NOT X-API-Key.
#   - Kafka/Redis have no ingress; resolve ClusterIP via kubectl (OrbStack routes
#     ClusterIPs from the host) with E2E_* env overrides.
#
# Usage:  source "$(dirname "$0")/lib/e2e_env.sh"   # or scripts/lib/e2e_env.sh
# Exports: NS, GATEWAY_URL, GATEWAY_API_KEY, AUTH_HEADER, KAFKA_BOOTSTRAP, REDIS_URL

NS="${NS:-multi-agent}"
KUBE="${KUBE:-kubectl}"

GATEWAY_URL="${OMNI_GATEWAY_URL:-${E2E_GATEWAY_URL:-http://gateway.ai-agent.local}}"

_e2e_secret_key() {
  "${KUBE}" get secret -n "${NS}" omni-gateway-secret \
    -o jsonpath='{.data.OMNI_GATEWAY_API_KEY}' 2>/dev/null | base64 -d 2>/dev/null
}

GATEWAY_API_KEY="${OMNI_GATEWAY_API_KEY:-${E2E_GATEWAY_API_KEY:-$(_e2e_secret_key)}}"
AUTH_HEADER="Authorization: Bearer ${GATEWAY_API_KEY}"

_e2e_cluster_ip() { # svc-name port
  local ip
  ip="$("${KUBE}" get svc -n "${NS}" "$1" -o jsonpath='{.spec.clusterIP}' 2>/dev/null)"
  [[ -n "${ip}" && "${ip}" != "None" ]] && echo "${ip}:$2"
}

KAFKA_BOOTSTRAP="${E2E_KAFKA_BOOTSTRAP:-${OMNI_KAFKA_BOOTSTRAP_SERVERS:-$(_e2e_cluster_ip kafka 9092)}}"
REDIS_URL="${E2E_REDIS_MA_URL:-${OMNI_REDIS_URL:-redis://$(_e2e_cluster_ip redis 6379)/0}}"

export NS GATEWAY_URL GATEWAY_API_KEY AUTH_HEADER KAFKA_BOOTSTRAP REDIS_URL

e2e_env_check() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' "${GATEWAY_URL}/healthz" || true)"
  echo "[e2e_env] gateway=${GATEWAY_URL} healthz=${code} kafka=${KAFKA_BOOTSTRAP} redis=${REDIS_URL} key_len=${#GATEWAY_API_KEY}"
  [[ "${code}" == "200" ]]
}
