#!/usr/bin/env bash
# Chẩn đoán vì sao PodMetrics / `kubectl top pod` trống dù `kubectl top node` có số liệu.
# Nguyên nhân thường gặp: kubelet /stats/summary trả mảng pods rỗng → metrics-server không có nguồn.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"

echo "=== 1) metrics-server ==="
"${KUBE}" get deploy -n kube-system metrics-server -o jsonpath='{.status.availableReplicas} available' 2>/dev/null || echo "(no deploy)"
echo ""

echo "=== 2) kubelet /stats/summary — số pod trong summary (nguồn PodMetrics) ==="
NODE="$("${KUBE}" get nodes -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "${NODE}" ]]; then
  echo "FAIL: no node"
  exit 1
fi
echo "node=${NODE}"

RAW="$("${KUBE}" get --raw "/api/v1/nodes/${NODE}/proxy/stats/summary" 2>/dev/null || echo "{}")"
echo "${RAW}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
pods = d.get('pods') or []
node = d.get('node') or {}
print('pods_in_summary:', len(pods))
print('node_cpu_present:', bool((node.get('cpu') or {})))
print('node_memory_present:', bool((node.get('memory') or {})))
if len(pods) == 0:
    print()
    print('VERDICT: kubelet không đưa pod vào summary → metrics-server không thể tạo PodMetrics.')
    print('OrbStack: Settings → Kubernetes → Kubelet Configuration — bật feature gate:')
    print('  featureGates:')
    print('    PodAndContainerStatsFromCRI: true')
    print('(apiVersion kubelet.config.k8s.io/v1beta1 / kind KubeletConfiguration). Khởi động lại cluster nếu cần.')
    print('Khác: nâng OrbStack/k3s; kiểm tra release notes nếu vẫn pods_in_summary=0.')
"

echo ""
echo "=== 3) So sánh nhanh ==="
"${KUBE}" top node 2>/dev/null || true
echo -n "kubectl top pod -A (first lines): "
"${KUBE}" top pod -A 2>&1 | head -5 || true
