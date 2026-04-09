# Báo cáo E2E: fault thật `nginx_waiting_fault` — phân tích log theo `trace_id`

**Ngày chạy:** 2026-04-08  
**Lệnh:** `SCENARIOS=nginx_waiting_fault bash scripts/e2e_incident_matrix.sh`  
**Kết quả script:** `passed` (scenario hoàn tất; script khôi phục ConfigMap + rollout `nginx-test`).  
**Trace ID:** `gw-prom-e57a0a2eca8f`  
**Bằng chứng JSON:** `reports/e2e-autonomous-evidence/run_20260408_121211.json`  
**Git tại thời điểm chạy:** `2e74974a8b28a6c2cab59c28cda94679135eee6d`

---

## 1. Tóm tắt điều hành (không che giấu)

| Câu hỏi | Trả lời có bằng chứng log |
|--------|---------------------------|
| Có fault thật trên cluster? | **Có** — pod `nginx-test-*` ở `CreateContainerConfigError` (thiếu ConfigMap `non-existent-config` do script xóa tạm). |
| Prober có chẩn đoán SDK khớp alert? | **Có** — `k8s_clinical_pod_status` / events ghi `CreateContainerConfigError`, events raw: `configmap "non-existent-config" not found`. |
| Có `EXECUTE_MUTATE` / rollout tự động? | **Không** trong trace này — không có dòng `event=action_emitted action=EXECUTE_MUTATE` và không có `omni_actions_in action=EXECUTE_MUTATE`. |
| Vì sao không mutate? | **Hai lần** chỉ `SUGGEST_REMEDIATION`; executor ghi `omni_actions_audit_only`. Lần sau `body_preview` nêu rõ **`ERR_REA_SIGMA_GATE_BLOCKED`** (cổng sigma trong `PROOF_OF_FAULT_GATE` chặn promote sang mutate). |
| LLM có đề xuất đúng tool? | **Có** — `event=agentic_mutate_plan_ok` với `tool=k8s_rollout_restart` (~51s sau RAG), nhưng emit ra vẫn là **SUGGEST** từ `PROOF_OF_FAULT_GATE`, không phải EXECUTE. |

**Kết luận:** Luồng này chứng minh **ingest → diagnóstic → RAG + agentic plan**; **không** chứng minh “tự sửa không can thiệp” trong lần chạy này vì **policy gate (sigma)** không cho phép bước mutate. Để đạt đúng kỳ vọng “tự fix”, cần điều chỉnh cấu hình gate / bằng chứng sigma trên lab hoặc tách kịch bản chứng minh chỉ khi `EXECUTE_MUTATE` được emit (theo `knownbase.md`).

---

## 2. Cách lấy lại log (Loki / kubectl)

**LogQL (Grafana Explore):**

```logql
{namespace="multi-agent", pod_name=~"omni-prober.*|omni-analyst.*|omni-executor.*"} |= "gw-prom-e57a0a2eca8f"
```

**kubectl (đối chiếu):**

```bash
./scripts/with_working_kube.sh logs -n multi-agent deploy/omni-prober --since=1h --tail=20000 | grep -F "gw-prom-e57a0a2eca8f"
./scripts/with_working_kube.sh logs -n multi-agent deploy/omni-analyst --since=1h --tail=20000 | grep -F "gw-prom-e57a0a2eca8f"
./scripts/with_working_kube.sh logs -n multi-agent deploy/omni-executor --since=1h --tail=20000 | grep -F "gw-prom-e57a0a2eca8f"
```

**Loki (lượt chạy):** script `gateway_alert_loki_verify.sh` báo **23 dòng** index, **3 stream** (prober / analyst / executor), khung thời gian mẫu ~0.6s cho phần đầu pipeline (đoạn sau agentic vẫn nằm trong cửa sổ query).

---

## 3. `omni-prober` — phân tích từng dòng (theo thứ tự thời gian)

Mỗi mục: **một dòng JSON log thật** (rút gọn phần `kafka_payload_preview` dài nếu cần) + **ý nghĩa**.

**P1 — `autonomy_contract` INGESTED**  
`transition=INGESTED … seq=1`  
→ Vòng tự trị bắt đầu: consumer đã nhận message với `trace_id` đúng.

**P2 — `request_trace` start_request**  
`phase=stream_consumer` … `alert_preview` có `alertname`, `namespace`, `pod`, `trace_id`  
→ Request gắn trace; alert là **NginxTestContainerWaitingFaultLab**, pod `nginx-test-74d655cbb-sq8kp`.

**P3 — `omni_worker` alert_kafka_in**  
`redis_msg_id=kafka-omni-alerts-0-11`  
→ Envelope đi từ Kafka topic `omni-alerts` (đúng MPV3).

**P4 — `omni_worker` stream_read**  
→ Đọc stream sau khi enqueue.

**P5 — `autonomy_contract` CONTEXT_READY seq=2**  
→ Context đủ để chạy pipeline trên prober.

**P6 — `diagnostic_dispatcher` diagnostic_dispatcher_plan**  
`plan=[... 'k8s_clinical_pod_events', 'k8s_resource_quota_probe']` (và probe status trước đó trong log đầy đủ)  
→ Kế hoạch tier2: **pod_state** + events + quota.

**P7 — `diagnostic_evidence_publish` probe=k8s_clinical_pod_status**  
`container_signals`: `nginx:waiting=CreateContainerConfigError`, `waiting_reasons`: CreateContainerConfigError, phase Pending  
→ **SDK khớp** tình trạng “container chờ — lỗi tạo container” (đúng fault inject).

**P8 — `diagnostic_evidence_publish` probe=k8s_clinical_pod_events**  
`raw` chứa: `Error: configmap "non-existent-config" not found`  
→ **Nguyên nhân vật lý** fault: ConfigMap thiếu (đúng kịch bản script).

**P9 — `diagnostic_evidence_publish` probe=k8s_resource_quota_probe**  
`(no ResourceQuota in namespace)`  
→ Loại trừ quota (bằng chứng phụ).

**P10 — `autonomy_contract` DIAGNOSED seq=5**  
→ Prober kết thúc giai đoạn chẩn đoán trên luồng của nó.

**P11 — `request_trace` end_request**  
`duration_ms=53.57`, `step='diagnostic_pipeline_completed'`  
→ Vòng consumer prober **đóng** trong ~54ms; evidence đã publish lên Kafka cho analyst.

---

## 4. `omni-analyst` — phân tích từng dòng

**A1–A3 — `autonomy_contract` CONTEXT_READY seq=3,4,6**  
→ Analyst nhận batch evidence; đồng bộ với các probe đã flush.

**A4 — `autonomy_contract` DIAGNOSED seq=7**  
→ Analyst coi đủ điều kiện “đã chẩn đoán” trên batch.

**A5 — `evidence_consumer` diag_batch_flush**  
`probes=['k8s_clinical_pod_status', 'k8s_clinical_pod_events', 'k8s_resource_quota_probe']`  
→ Khớp với prober; **một batch** thống nhất.

**A6 — `pkg.rag.gate` rag_gate_hit**  
`collection=k8s_expert best_score=0.6808`  
→ RAG hit trước khi quyết định hành động.

**A7 — `evidence_consumer` rag_truth_citations**  
`chunk_ids=[...]`  
→ Trích dẫn corpus đã chọn (phục vụ audit / giải thích).

**A8 — `evidence_consumer` action_emitted**  
`action=SUGGEST_REMEDIATION source=RAG_HIT`  
→ **Nhánh RAG** đầu tiên chỉ **đề xuất**, không mutate.

**A9 — `autonomy_contract` PLAN_EMITTED seq=8**  
→ Plan (suggest) đã được đưa vào pipeline.

**A10 — `analyst_agentic_loop` agentic_mutate_plan_ok**  
`tool=k8s_rollout_restart` `model=deepseek-r1:8b`  
→ **Planner** thống nhất: nên rollout restart deployment (đúng hướng “sửa” workload sau khi restore ConfigMap). Đây là bằng chứng **ý định** mutate đúng tool.

**A11 — `evidence_consumer` action_emitted**  
`action=SUGGEST_REMEDIATION source=PROOF_OF_FAULT_GATE`  
→ **Không** emit `EXECUTE_MUTATE`; cổng proof-of-fault chỉ **đẩy suggest** (kèm lý do gate — thấy rõ ở executor).

**A12 — `autonomy_contract` PLAN_EMITTED seq=10**  
→ Vòng plan thứ hai (suggest) sau agentic.

---

## 5. `omni-executor` — phân tích từng dòng

**E1 — `autonomy_contract` PLAN_EMITTED**  
`component=kafka_actions_consumer seq=9`  
→ Executor tham gia contract (sequence).

**E2 — `kafka_actions_consumer` omni_actions_in**  
`action=SUGGEST_REMEDIATION` … `Source: RAG_HIT` … `Suggested tool: kubectl_get_events`  
→ **Chỉ đọc** hành động suggest (RAG text); chưa mutate.

**E3 — `kafka_actions_consumer` omni_actions_audit_only**  
`(no execute)`  
→ **Xác nhận:** `OMNI_AUTO_EXECUTE_ENABLED` không giúp nếu action là **SUGGEST** — không có mutate để thực thi.

**E4 — `autonomy_contract` PLAN_EMITTED seq=11**  
→ Lần suggest thứ hai.

**E5 — `kafka_actions_consumer` omni_actions_in**  
`body_preview=... ERR_REA_SIGMA_GATE_BLOCKED ... Source: PROOF_OF_FAULT_GATE Suggested tool: inspect_pod_details`  
→ **Đây là dòng chứng minh gate:** mutate bị chặn bởi **sigma gate** trong bằng chứng; hệ thống **không** promote `EXECUTE_MUTATE`.

**E6 — `kafka_actions_consumer` omni_actions_audit_only**  
`(no execute)`  
→ Lần hai vẫn chỉ audit.

---

## 6. Bảng tổng hợp “đã có / chưa có”

| Kỳ vọng | Trong log trace này |
|--------|----------------------|
| Fault thật (ConfigMap) | Có (P8) |
| SDK evidence | Có (P7–P9) |
| RAG hit | Có (A6–A7) |
| Planner đề xuất `k8s_rollout_restart` | Có (A10) |
| `EXECUTE_MUTATE` | **Không** |
| `action_feedback` sau mutate | **Không** (không có mutate) |

---

## 7. Phụ lục — file JSON báo cáo matrix

`reports/e2e-autonomous-evidence/run_20260408_121211.json` ghi `scenario=nginx_waiting_fault`, `status=passed`, `trace_id=gw-prom-e57a0a2eca8f`. **Passed** ở đây nghĩa là **script E2E hoàn tất** (inject → gateway → verify → restore), **không** đồng nghĩa “đã mutate thành công” — điều đó phải đọc log như trên.

---

*Báo cáo này dựa trên log trực tiếp từ cluster (`kubectl logs`) tại thời điểm chạy; tái hiện bằng cách lặp lại lệnh §2 với cùng `trace_id` nếu log vẫn còn trong retention.*
