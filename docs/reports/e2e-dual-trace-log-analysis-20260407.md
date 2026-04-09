# Báo cáo E2E: phân tích log theo từng dòng — hai `trace_id`

**Ngày:** 2026-04-07  
**Nguồn log:** `kubectl logs deploy/{omni-prober,omni-analyst,omni-executor} -n multi-agent` + grep `trace_id` (OrbStack / `multi-agent`).  
**Lệnh E2E gốc:**  
- `STRICT_ASSERT=0 bash scripts/gateway_alert_loki_verify.sh` → **Trace A**  
- `SCENARIOS=nginx_waiting_fault STRICT_ASSERT=0 bash scripts/e2e_incident_matrix.sh` → **Trace B**

| Trace | Ý nghĩa kịch bản |
|-------|------------------|
| **A** `gw-prom-d7796b45517d` | Lab **HighCPUUsage** vs pod **Running** + CPU thực tế thấp → **STATE_MACHINE_CONTRAST** (mismatch alert / SDK). |
| **B** `gw-prom-21e83e390b09` | Fault thật: **CreateContainerConfigError** / ConfigMap `non-existent-config` không tồn tại (nginx-test inject). |

**LogQL (đối chiếu Loki):**

```logql
{namespace="multi-agent", pod_name=~"omni-prober.*|omni-analyst.*|omni-executor.*"} |= "gw-prom-d7796b45517d"
```

(thay suffix bằng `gw-prom-21e83e390b09` cho Trace B).

---

## Trace A — `gw-prom-d7796b45517d` (gateway HighCPU lab)

### Đánh giá ngắn

- **Có đúng luồng MPV3:** Kafka `omni-alerts` → prober probe → `omni-diagnostic-evidence` → analyst `evidence_consumer` → `omni-actions` → executor **audit-only** `SUGGEST`.
- **Kết luận nghiệp vụ:** Alert mô tả CPU ~90% nhưng SDK + Prom đều cho thấy CPU **không** cao → **`STATE_MACHINE_CONTRAST`** là **đúng** cho kịch bản lab mismatch; **không** có `EXECUTE_MUTATE`.

### `omni-prober` — 13 dòng (theo thứ tự thời gian)

| # | Logger / transition | Ý nghĩa |
|---|---------------------|--------|
| A1 | `autonomy_contract` `transition=INGESTED` `seq=1` | Bắt đầu state machine; message đã vào consumer với `trace_id` cố định. |
| A2 | `request_trace` `start_request` `phase=stream_consumer` | Request gắn trace; `alertname=HighCPUUsage`, `pod=nginx-test-85bd9988c-dpc4j`. |
| A3 | `omni_worker` `alert_kafka_in` `redis_msg_id=kafka-omni-alerts-0-13` | Envelope từ topic **`omni-alerts`** (đúng split topology). |
| A4 | `omni_worker` `stream_read` | Xác nhận đọc sau enqueue. |
| A5 | `autonomy_contract` `CONTEXT_READY` `seq=2` | Prober sẵn sàng chạy diagnostic. |
| A6 | `diagnostic_dispatcher` `diagnostic_dispatcher_plan` `mode=workload_resource` | Kế hoạch tier2: **tài nguyên workload** (metrics + Prom), không phải pod_state thuần. |
| A7 | `diagnostic_evidence_publish` `probe=k8s_clinical_pod_status` | SDK: **phase=Running**, không waiting/crash → **không** khớp “đang nóng” theo nghĩa sự cố container. |
| A8 | `probe=k8s_clinical_pod_metrics` | PodMetrics: `cpu=80194n` (~0.08m cores), **rất thấp** so với mô tả alert ~90%. |
| A9 | `probe=k8s_clinical_pod_log_tail` **SKIPPED** | Rule lâm sàng: pod “healthy” → không bắt buộc tail log. |
| A10 | `probe=prom_pod_cpu_cores` | Prom: `s0≈0.00013` cores (rate 5m) — **xác nhận** CPU không bão hòa. |
| A11 | `probe=prom_pod_memory_wss` | WSS ~3.8MB — phụ, không mâu thuẫn với CPU mismatch. |
| A12 | `autonomy_contract` `DIAGNOSED` `seq=3` (prober) | Vòng probe trên prober **hoàn tất**. |
| A13 | `request_trace` `end_request` `duration_ms≈77` | Prober đóng request sau pipeline diagnostic. |

### `omni-analyst` — 9 dòng

| # | Nội dung | Ý nghĩa |
|---|----------|--------|
| B1–B5 | `CONTEXT_READY` `seq=4…8` | **Năm** batch evidence tương ứng **năm** probe (status, metrics, log skip, prom CPU, prom mem) — flush theo từng mẩu Kafka. |
| B6 | `DIAGNOSED` `seq=9` | Analyst đã nhận đủ batch để suy luận. |
| B7 | `evidence_consumer` `diag_batch_flush` | Liệt kê đúng 5 probe trong batch. |
| B8 | `action_emitted` `SUGGEST_REMEDIATION` **`source=STATE_MACHINE_CONTRAST`** | **Điểm quyết định:** không promote mutate; chẩn đoán **mâu thuẫn alert vs trạng thái sống**. |
| B9 | `PLAN_EMITTED` `seq=10` | Hành động gợi ý đã publish lên `omni-actions`. |

### `omni-executor` — 3 dòng

| # | Nội dung | Ý nghĩa |
|---|----------|--------|
| C1 | `PLAN_EMITTED` `kafka_actions_consumer` `seq=11` | Executor nhận message cùng trace. |
| C2 | `omni_actions_in` `SUGGEST_REMEDIATION` `body_preview` | English: alert không nhất quán với PodMetrics / stale series — **khớp** intent contrast. |
| C3 | `omni_actions_audit_only` | **Không** thực thi mutate (đúng policy suggest-only trên luồng này). |

---

## Trace B — `gw-prom-21e83e390b09` (nginx_waiting_fault — fault thật)

### Đánh giá ngắn

- **Prober:** Bằng chứng **vật lý** đầy đủ: Pending, `CreateContainerConfigError`, events **`configmap "non-existent-config" not found`** — **đúng** fault inject.
- **Analyst:** RAG hit → `SUGGEST` từ **RAG_HIT**; sau đó **~69s** agentic loop đề xuất `k8s_rollout_restart`, rồi **`PROOF_OF_FAULT_GATE`** emit thêm `SUGGEST` với **`ERR_REA_SIGMA_GATE_BLOCKED`**.
- **Đánh giá:** Luồng **ingest + SDK + RAG** khớp fault thật. Tuy nhiên **lần hành động thứ hai** vẫn ghi **sigma gate blocked** — điều này **phù hợp** với build **trước** three-lane **state fast-track**, hoặc image trên cluster **chưa** chứa code mới / Redis baseline không có `dr`/z trong cửa sổ quan sát. Để **state lane** bỏ sigma trong bản mới, cần **image worker đã build** + rollout và **xác minh** meta `proof_lane=state` trong log (khi thêm log field đó ở runtime).

### `omni-prober` — 11 dòng

| # | Probe / transition | Ý nghĩa |
|---|---------------------|--------|
| T1 | `INGESTED` `seq=1` | Ingest OK. |
| T2 | `start_request` | `alertname=NginxTestContainerWaitingFaultLab`, pod fault `nginx-test-bffdfdd8-t6fpp`. |
| T3 | `alert_kafka_in` `kafka-omni-alerts-0-14` | Cùng topic chuẩn. |
| T4 | `stream_read` | Đọc message. |
| T5 | `CONTEXT_READY` `seq=2` | Sẵn sàng probe. |
| T6 | `diagnostic_dispatcher_plan` `mode=pod_state` | Đúng lớp: **trạng thái pod/container** (không ưu tiên CPU metric trước). |
| T7 | `k8s_clinical_pod_status` | **Pending**, `nginx:waiting=CreateContainerConfigError` — **khớp** alert lab. |
| T8 | `k8s_clinical_pod_events` | **Failed: configmap "non-existent-config" not found** — **nguyên nhân gốc** fault. |
| T9 | `k8s_resource_quota_probe` | Không có ResourceQuota — loại trừ hết quota namespace. |
| T10 | `DIAGNOSED` `seq=6` | Probe xong trên prober. |
| T11 | `end_request` `duration_ms≈22` | Prober đóng nhanh (ít probe hơn trace A). |

### `omni-analyst` — 12 dòng

| # | Nội dung | Ý nghĩa |
|---|----------|--------|
| U1–U3 | `CONTEXT_READY` `seq=3,4,5` | Ba batch cho ba probe (status, events, quota). |
| U4 | `DIAGNOSED` `seq=7` | *(Seq 6 trên prober; analyst nhảy seq — không ảnh hưởng semantics.)* |
| U5 | `diag_batch_flush` | Đúng bộ probe **pod_state**. |
| U6 | `pkg.rag.gate` `rag_gate_hit` `best_score≈0.664` | RAG trả hit `k8s_expert`. |
| U7 | `rag_truth_citations` + `chunk_ids` | Trích dẫn cụ thể (audit được). |
| U8 | `action_emitted` **`source=RAG_HIT`** | Lần đầu: suggest từ RAG (body dài, có đoạn “sách giáo khoa” Kubernetes — **chất lượng hiển thị** có thể cải thiện bằng prompt/rerank). |
| U9 | `PLAN_EMITTED` `seq=8` | Publish action đầu. |
| U10 | `analyst_agentic_loop` **`agentic_mutate_plan_ok`** `tool=k8s_rollout_restart` | Agentic đề xuất **rollout restart** — **hợp lý** cho pod kẹt tạo container. |
| U11 | `action_emitted` **`source=PROOF_OF_FAULT_GATE`** | Lần hai: gate can thiệp sau agentic — **body** (xem executor) báo **sigma blocked**. |
| U12 | `PLAN_EMITTED` `seq=10` | Publish action thứ hai. |

### `omni-executor` — 6 dòng

| # | Nội dung | Ý nghĩa |
|---|----------|--------|
| V1 | `PLAN_EMITTED` `seq=9` | Nhận **SUGGEST** đầu (RAG). |
| V2 | `omni_actions_in` `Source: RAG_HIT` | Khớp U8; `Suggested tool: kubectl_get_events` — chấp nhận được nhưng body **dài/dư** context. |
| V3 | `audit_only` | Không execute. |
| V4 | `PLAN_EMITTED` `seq=11` | Nhận **SUGGEST** thứ hai (~69s sau). |
| V5 | `body_preview` **`ERR_REA_SIGMA_GATE_BLOCKED`** **`PROOF_OF_FAULT_GATE`** | **Không** promote mutate; gợi ý `inspect_pod_details`. |
| V6 | `audit_only` | Không execute. |

---

## Kết luận chung

1. **Trace A** chứng minh **đường contrast** lab (alert “nóng” vs thực tế “lạnh”) **hoạt động** và **trace_id** xuyên suốt prober → analyst → executor.  
2. **Trace B** chứng minh **SDK + events** bắt đúng **ConfigMap missing**; RAG + agentic **nhận diện** hướng xử lý (rollout). Tầng **proof gate** vẫn trả **`ERR_REA_SIGMA_GATE_BLOCKED`** trên lần emit thứ hai — cần đối chiếu **version image** trên cluster và **cấu hình** `OMNI_PROOF_LANE_ENABLED` / **state lane** nếu kỳ vọng **không** chặn mutate bằng sigma cho fault trạng thái.  
3. **Khuyến nghị vận hành:** Sau mỗi `docker build`, **import image** vào runtime cluster hoặc bump tag + `imagePullPolicy` để pod **thực sự** chạy binary mới; sau đó grep log thêm `proof_lane` / `sigma_bypass_reason` (khi đã có trong build).
