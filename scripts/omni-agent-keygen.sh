#!/usr/bin/env bash
# Tạo API key cho Omni Remote Agent và lưu vào K8s secret omni-gateway-secret.
#
# Gateway deployment đã có secretRef: omni-gateway-secret (optional=true)
# → chỉ cần tạo secret đúng tên, gateway tự nhận OMNI_GATEWAY_API_KEY khi restart.
#
# Usage:
#   bash scripts/omni-agent-keygen.sh [--namespace NS] [--dry-run]

set -euo pipefail

NS="${NAMESPACE:-multi-agent}"
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) NS="$2"; shift 2 ;;
    --dry-run)   DRY_RUN=true; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

run() { $DRY_RUN && echo "[DRY-RUN] $*" || "$@"; }

# ─── 1. Generate key ──────────────────────────────────────────────────────────
API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

# ─── 2. Tạo / cập nhật secret omni-gateway-secret ────────────────────────────
echo "Tạo K8s secret omni-gateway-secret (namespace: ${NS})..."
run kubectl create secret generic omni-gateway-secret \
  --from-literal=OMNI_GATEWAY_API_KEY="$API_KEY" \
  -n "$NS" \
  --dry-run=client -o yaml | run kubectl apply -f -

# ─── 3. Rollout gateway để pick up secret mới ─────────────────────────────────
echo "Rollout omni-gateway để nhận secret..."
run kubectl rollout restart deployment/omni-gateway -n "$NS"

if ! $DRY_RUN; then
  kubectl rollout status deployment/omni-gateway -n "$NS" --timeout=90s
fi

# ─── 4. Print thông tin cài đặt ───────────────────────────────────────────────
echo ""
echo "======================================================="
echo " Omni Remote Agent — Thông tin kết nối"
echo "======================================================="
echo ""
echo " GATEWAY URL (internal lab):"
echo "   http://gateway.ai-agent.local"
echo ""
echo " GATEWAY URL (trong cluster K8s):"
echo "   http://omni-gateway.${NS}.svc.cluster.local"
echo ""
echo " API KEY:"
echo "   $API_KEY"
echo ""
echo " Cài agent lên server khách hàng:"
echo ""
echo "   scp dist/omni-agent-1.0.0.tar.gz user@server:/tmp/"
echo "   ssh user@server 'cd /tmp && tar -xzf omni-agent-1.0.0.tar.gz && \\"
echo "     sudo bash omni-agent-1.0.0/install.sh \\"
echo "       --gateway-url http://gateway.ai-agent.local \\"
echo "       --api-key $API_KEY'"
echo ""
echo " Kiểm tra agent đã kết nối:"
echo "   curl -s -H 'Authorization: Bearer $API_KEY' \\"
echo "     http://gateway.ai-agent.local/agents/remote | python3 -m json.tool"
echo ""
echo " K8s secret:"
echo "   kubectl get secret omni-gateway-secret -n ${NS}"
echo "   kubectl get secret omni-gateway-secret -n ${NS} -o jsonpath='{.data.OMNI_GATEWAY_API_KEY}' | base64 -d"
echo "======================================================="
