#!/usr/bin/env bash
# Portal E2E release gate — chạy bộ Playwright tests/e2e_portals lên provider/tenant
# portal THẬT (aoip-provider-web + aoip-tenant-web + aoip-provider-portal +
# aoip-tenant-portal BFF), qua Traefik LB tại provider.ai-agent.local /
# tenant.ai-agent.local (yêu cầu /etc/hosts đã map — make hosts-update).
#
# Kế thừa từ omni-ui portal gate (retired 2026-07-06, xem
# docs/handoffs/CURRENT_SESSION.md) — omni-ui đã bị xoá khỏi cluster, portal
# thật hiện tại là provider/tenant Next.js apps dưới ui/apps/.
#
# KHÔNG chạy trong CI thuần — cần cluster thật (kubectl context trỏ lab OrbStack)
# và /etc/hosts đã map domain provider/tenant/dex về LB IP.
set -euo pipefail

NS="${NS:-multi-agent}"
READY_TIMEOUT_S="${READY_TIMEOUT_S:-60}"
E2E_DIR="$(cd "$(dirname "$0")/../tests/e2e_portals" && pwd)"

log() { printf '[portal-e2e-gate] %s\n' "$*" >&2; }
fail() { log "FAIL: $*"; exit 1; }

command -v kubectl >/dev/null || fail "kubectl không có trong PATH"
command -v npx >/dev/null || fail "npx không có trong PATH"
[ -d "$E2E_DIR/node_modules" ] || fail "tests/e2e_portals/node_modules chưa cài — chạy: cd tests/e2e_portals && npm ci"

for deploy in aoip-provider-web aoip-tenant-web aoip-provider-portal aoip-tenant-portal aoip-dex; do
  log "Preflight: kiểm tra deploy/$deploy trong namespace $NS"
  kubectl -n "$NS" get "deploy/$deploy" >/dev/null || fail "không truy cập được cluster/deploy $deploy"
  kubectl -n "$NS" rollout status "deploy/$deploy" --timeout="${READY_TIMEOUT_S}s" >/dev/null \
    || fail "deploy/$deploy chưa Ready"
done

log "Chờ provider portal sẵn sàng (http://provider.ai-agent.local/, timeout ${READY_TIMEOUT_S}s)"
deadline=$((SECONDS + READY_TIMEOUT_S))
until curl -sf -o /dev/null "http://provider.ai-agent.local/"; do
  [ $SECONDS -lt $deadline ] || fail "provider.ai-agent.local không trả lời sau ${READY_TIMEOUT_S}s (kiểm tra /etc/hosts + Traefik LB IP — make hosts-update)"
  sleep 2
done

log "Chạy Playwright (tests/e2e_portals)"
(
  cd "$E2E_DIR"
  npx playwright test
)
log "PASS: portal E2E gate xanh"
