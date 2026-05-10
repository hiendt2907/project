# Phase 0 — Checklist chuẩn bị (staging / lab)

**Owner:** _________________ **Ngày:** _________________ **Cluster / context:** _________________

| # | Hạng mục | OK | Ghi chú |
|---|----------|----|---------|
| 1 | Namespace fault injection xác định (`FAULT_NS`, mặc định `multi-agent`) | [ ] | |
| 2 | Deployment mục tiêu tồn tại (`kubectl get deploy -n $NS`) | [ ] | vd. `nginx-test` |
| 3 | `autonomous_allowed_namespaces` trong ConfigMap khớp namespace test | [ ] | `k8s/deployments/omni-worker-configmap.yaml` |
| 4 | `OMNI_AUTO_EXECUTE_ENABLED=false` (hoặc chỉ bật trong cửa sổ có approval) | [ ] | |
| 5 | Loki reachable từ cluster (`http://loki.monitor.svc.cluster.local:3100` hoặc URL lab) | [ ] | |
| 6 | Trace grep: biết LogQL mẫu — xem [rag-gate-observability.md](../rag-gate-observability.md) | [ ] | |
| 7 | RAG: corpus / ingest tối thiểu cho triệu chứng dự kiến (nếu test C) | [ ] | Không hardcode secret |
| 8 | RBAC: SA worker đủ quyền probe trong namespace | [ ] | |
| 9 | Rollback plan: `scripts/alert_flow_realistic/inject_fault_restore.sh` đã đọc | [ ] | |

**Sign-off:** _________________ (SRE / owner)
