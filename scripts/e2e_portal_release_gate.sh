#!/usr/bin/env bash
# Portal E2E release gate — chạy bộ Playwright ui/e2e lên pod omni-ui THẬT.
#
# Tự động hoá quy trình thủ công trong docs/handoffs/CURRENT_SESSION.md:
#   1. Preflight: kubectl truy cập được, deploy/omni-ui Ready.
#   2. Lấy credentials từ secret omni-ui-secrets (không hardcode).
#   3. Port-forward svc/omni-ui về 127.0.0.1:$LOCAL_PORT (tự dọn khi thoát).
#   4. Chờ UI trả lời qua đúng Host header (omni.ai-agent.local).
#   5. cd ui && npm run e2e — exit code của Playwright là exit code của gate.
#
# Mặc định CHỈ read-flow (write-flow answer-question bị skip). Muốn chạy cả
# write-flow (mutation thật vào Redis của tenant lab): E2E_ALLOW_WRITE=1.
#
# Yêu cầu: kubectl context trỏ lab OrbStack; ui/node_modules đã cài
# (npm ci trong ui/ nếu chưa). KHÔNG chạy trong CI thuần — cần cluster thật.
set -euo pipefail

NS="${NS:-multi-agent}"
LOCAL_PORT="${LOCAL_PORT:-18081}"
E2E_HOST="${E2E_HOST:-omni.ai-agent.local}"
READY_TIMEOUT_S="${READY_TIMEOUT_S:-60}"
UI_DIR="$(cd "$(dirname "$0")/../ui" && pwd)"

log() { printf '[portal-e2e-gate] %s\n' "$*" >&2; }
fail() { log "FAIL: $*"; exit 1; }

command -v kubectl >/dev/null || fail "kubectl không có trong PATH"
command -v npm >/dev/null || fail "npm không có trong PATH"
[ -d "$UI_DIR/node_modules" ] || fail "ui/node_modules chưa cài — chạy: cd ui && npm ci"

log "Preflight: kiểm tra deploy/omni-ui trong namespace $NS"
kubectl -n "$NS" get deploy/omni-ui >/dev/null || fail "không truy cập được cluster/deploy omni-ui"
kubectl -n "$NS" rollout status deploy/omni-ui --timeout=120s >/dev/null \
  || fail "deploy/omni-ui chưa Ready"

log "Lấy credentials từ secret omni-ui-secrets"
E2E_USERNAME="$(kubectl -n "$NS" get secret omni-ui-secrets -o jsonpath='{.data.ADMIN_USERNAME}' | base64 -d)"
E2E_PASSWORD="$(kubectl -n "$NS" get secret omni-ui-secrets -o jsonpath='{.data.ADMIN_PASSWORD}' | base64 -d)"
[ -n "$E2E_USERNAME" ] && [ -n "$E2E_PASSWORD" ] || fail "ADMIN_USERNAME/ADMIN_PASSWORD rỗng trong secret"

if lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  fail "port $LOCAL_PORT đang bận — dừng port-forward cũ hoặc đặt LOCAL_PORT khác"
fi

log "Port-forward svc/omni-ui $LOCAL_PORT:80"
kubectl -n "$NS" port-forward svc/omni-ui "$LOCAL_PORT:80" >/dev/null 2>&1 &
PF_PID=$!
cleanup() { kill "$PF_PID" 2>/dev/null || true; wait "$PF_PID" 2>/dev/null || true; }
trap cleanup EXIT

log "Chờ UI sẵn sàng (Host: $E2E_HOST, timeout ${READY_TIMEOUT_S}s)"
deadline=$((SECONDS + READY_TIMEOUT_S))
until curl -sf -o /dev/null -H "Host: $E2E_HOST" "http://127.0.0.1:$LOCAL_PORT/login"; do
  [ $SECONDS -lt $deadline ] || fail "UI không trả lời /login sau ${READY_TIMEOUT_S}s"
  kill -0 "$PF_PID" 2>/dev/null || fail "port-forward chết sớm"
  sleep 2
done

WRITE_MODE="${E2E_ALLOW_WRITE:-}"
if [ -n "$WRITE_MODE" ]; then
  log "Chạy Playwright (write-flow: ENABLED — mutation thật vào tenant lab)"
else
  log "Chạy Playwright (write-flow: skipped — bật bằng E2E_ALLOW_WRITE=1)"
fi
(
  cd "$UI_DIR"
  export E2E_USERNAME E2E_PASSWORD
  export E2E_BASE_URL="http://$E2E_HOST:$LOCAL_PORT"
  [ -n "$WRITE_MODE" ] && export E2E_ALLOW_WRITE="$WRITE_MODE"
  npm run e2e
)
log "PASS: portal E2E gate xanh"
