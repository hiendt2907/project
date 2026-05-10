# Kế hoạch kiểm thử luồng alert thực (ngoài lab synthetic)

**Mục tiêu:** Bổ sung bằng chứng cho hành vi Omni khi **có tín hiệu giống production** — không thay thế smoke `gateway_alert_loki_verify.sh`, mà **ép các lớp** mà lab không cover (TRUE incident, RAG miss/hit thật, escalate/mutate có kiểm soát).

**Tham chiếu:** [project-memory.md](project-memory.md) `LabVsRealAlertTesting`, [OMNI_PROJECT_CANONICAL.md](../vendor/OMNI_PROJECT_CANONICAL.md), `.cursor/rules/omni-cicd-k8s.mdc`.

---

## 1. Phân loại kịch bản (định nghĩa trước khi đo)

| Mã | Ý nghĩa | Ví dụ kiểm chứng |
|----|---------|------------------|
| **A — Alert sai / stale** | Prometheus/AM báo nhưng SDK không khớp (lab nginx HighCPU) | Đã có smoke; chỉ cần giữ regression |
| **B — Alert đúng, sự cố thật** | Pod crash, OOM, throttling thật — SDK + log khớp alert | **Chưa** có trong smoke mặc định — **ưu tiên kế hoạch** |
| **C — RAG** | Cần corpus khớp triệu chứng; miss → SDK-only LLM | Đo trên staging với chunk đã ingest |
| **D — Mutate** | Chỉ khi gate + allowlist + policy | Sandbox / staging trước prod |

Ghi rõ mã trong báo cáo run để không nhầm với “FALSE_NEGATIVE” của case A.

---

## 2. Giai đoạn 0 — Chuẩn bị (1–2 ngày)

1. **Namespace / RBAC:** Xác định cluster target (staging), namespace cho fault injection, `autonomous_allowed_namespaces` khớp.
2. **Observability:** Loki query sẵn; trace_id grep; dashboard Omni Learning nếu có.
3. **Dữ liệu:** RAG corpus tối thiểu cho alertname triệu chứng B (ingest có kiểm soát).
4. **An toàn:** `OMNI_AUTO_EXECUTE_ENABLED=false` trừ khi có cửa sổ test; mutate chỉ staging + approval.

**Exit criteria:** Checklist env ký tên; owner runbook.

**Repo — checklist in-repo:** [alert-flow-realistic/PHASE0_CHECKLIST.md](alert-flow-realistic/PHASE0_CHECKLIST.md) (điền owner/ngày khi chạy).

---

## 3. Giai đoạn 1 — Fault có kiểm soát trên staging (kịch bản B)

**Ý tưởng:** Gây lỗi **thật** trong pod workload (crash loop, exit non-zero, stress CPU có giới hạn) sao cho **Prometheus rule** và **SDK probe** cùng nhìn thấy tình trạng xấu — khác với case A (mismatch).

| Bước | Việc làm | Bằng chứng |
|------|----------|------------|
| 1.1 | Chọn deployment non-prod; scale hoặc patch image bad / command exit 1 | Alert firing đồng nhất với `k8s_clinical_pod_status` / events |
| 1.2 | POST Alertmanager-style payload **hoặc** để rule tự fire vào gateway (cùng label namespace/pod) | `trace_id` end-to-end |
| 1.3 | Loki: prober → analyst → (executor nếu có action) | Log line `diag_batch_flush`, `RAG_HIT` hoặc `SDK_ONLY`, **không** chỉ `STATE_MACHINE_CONTRAST` mismatch |
| 1.4 | Ghi **artifact:** JSON trace summary (trace_id, alertname, source, verdict) | File trong `reports/` hoặc ticket |

**Exit criteria:** Ít nhất **2** loại fault khác nhau (vd. CrashLoop vs OOM) pass qua pipeline với chẩn đoán **khớp evidence**.

**Rủi ro:** Stress ảnh hưởng node — dùng limit/quota.

**Repo — automation:** [scripts/alert_flow_realistic/README.md](../../scripts/alert_flow_realistic/README.md) (`inject_fault_crashloop.sh`, `inject_fault_oom.sh`, `inject_fault_restore.sh`, `post_gateway_alert.sh`); template artifact: [reports/alert-flow-realistic/artifact_template.json](../../reports/alert-flow-realistic/artifact_template.json). Chạy fault → POST hoặc đợi rule → điền artifact → lưu bản copy dưới `reports/alert-flow-realistic/runs/` (gitignore nếu chứa dữ liệu nhạy cảm).

---

## 4. Giai đoạn 2 — Replay payload từ incident (anonymized)

**Ý tưởng:** Lấy envelope Prometheus/AM đã xảy ra thật (đã **strip secret / IP / customer**), replay qua gateway trong staging.

| Bước | Việc làm | Bằng chứng |
|------|----------|------------|
| 2.1 | Thu thập 1–3 mẫu từ incident đã đóng | Lưu `scripts/alert_payloads/replay/replay_*.json` (không secret) — xem [replay/README.md](../../scripts/alert_payloads/replay/README.md) |
| 2.2 | `curl` hoặc wrapper script → gateway webhook | So sánh với hành vi lúc incident (notes) |
| 2.3 | Cập nhật **golden test** hoặc matrix row nếu phát hiện gap classify | PR nhỏ |

**Exit criteria:** Mỗi mẫu có **expected** source (RAG_HIT / SDK / escalate) ghi trong file JSON comment hoặc test.

---

## 5. Giai đoạn 3 — Soak / trùng lặp alert (tùy chọn)

**Ý tưởng:** Kiểm tra dedupe, rate, DLQ khi cùng incident spam 5–10 phút.

| Bước | Việc làm |
|------|----------|
| 3.1 | Script gửi lặp payload cùng `group_key` / labels (nếu gateway hỗ trợ) | [scripts/alert_payload_soak.sh](../../scripts/alert_payload_soak.sh) (`REPEAT`, `INTERVAL_SEC`) |
| 3.2 | Quan sát Kafka lag, consumer, không double mutate |

**Exit criteria:** Không có storm không kiểm soát; tài liệu hóa nếu cần dedupe sau này.

---

## 6. Giai đoạn 4 — Production read-only (shadow)

**Chỉ khi policy cho phép:** ingest alert vào Omni **không** auto-mutate; chỉ log + Loki + soát sau.

| Bước | Việc làm |
|------|----------|
| 4.1 | Mirror AM → webhook staging trước |
| 4.2 | So khớp với hành động SRE thực tế (manual review) |

**Repo:** Không tự động hóa mirror AM — chỉ quy trình. Bắt buộc policy nội bộ + staging đã pass Phase 1–2.

---

## 7. Tiêu chí nghiệm thu tổng

### 7a. Deliverables trong repo (đã giao)

- [x] Checklist Phase 0: [alert-flow-realistic/PHASE0_CHECKLIST.md](alert-flow-realistic/PHASE0_CHECKLIST.md)
- [x] Script fault + POST + soak: [scripts/alert_flow_realistic/README.md](../../scripts/alert_flow_realistic/README.md), [scripts/alert_payload_soak.sh](../../scripts/alert_payload_soak.sh)
- [x] Template artifact: [reports/alert-flow-realistic/artifact_template.json](../../reports/alert-flow-realistic/artifact_template.json)
- [x] Replay mẫu: [scripts/alert_payloads/replay/](../../scripts/alert_payloads/replay/)

### 7b. Cổng thủ công trên cluster (owner chạy và tick)

- [ ] Ít nhất **một** kịch bản **B** (fault thật staging) có báo cáo artifact + trace Loki.
- [ ] Ít nhất **một** replay **2** (payload anonymized) nếu có nguồn.
- [ ] Matrix / registry: `config/incident_training_matrix.yaml` hoặc golden test cập nhật khi phát hiện sai lệch.

---

## 8. Lệnh tham chiếu nhanh

```bash
# Smoke (lab — case A); NS bắt buộc (multi-agent trong Makefile lab)
export NS=multi-agent
bash scripts/gateway_alert_loki_verify.sh
STRICT_ASSERT=0 bash scripts/gateway_alert_loki_verify.sh   # nếu strict grep gây nhiễu

# Matrix / gate (make sets NS=multi-agent)
make e2e-incident-matrix   # hoặc NS=multi-agent bash scripts/e2e_incident_matrix.sh

# Phase 1 — fault + gateway (sau Phase 0)
# FAULT_NS=multi-agent FAULT_DEPLOY=nginx-test ./scripts/alert_flow_realistic/inject_fault_crashloop.sh
# NS=multi-agent ./scripts/alert_flow_realistic/post_gateway_alert.sh scripts/alert_payloads/alertmanager_nginx_waiting_fault.json
# ./scripts/alert_flow_realistic/inject_fault_restore.sh

# Phase 3 — soak
# NS=multi-agent REPEAT=15 INTERVAL_SEC=5 ./scripts/alert_payload_soak.sh scripts/alert_payloads/replay/replay_example_minimal.json
```

---

## 9. Lịch đề xuất

| Tuần | Trọng tâm | Trạng thái repo |
|------|-----------|-----------------|
| 0 | Scaffold checklist + scripts + template | Hoàn thành (§7a) |
| 1 | Giai đoạn 0 sign-off + 1.1–1.2 (một fault) | Chờ staging |
| 2 | 1.3–1.4 artifact + fault thứ hai | Chờ staging |
| 3 | Giai đoạn 2 nếu có payload; hoặc 3 soak | Chờ vận hành |

*Cập nhật §7b/§9 khi owner hoàn thành cổng thủ công.*

---

## 10. Phase 4 — Runbook tóm tắt (policy)

1. Chỉ bật khi Phase **7b** có ít nhất một dòng đã tick và SRE phê duyệt.
2. Alertmanager production: route `receiver` tới webhook **staging** hoặc mirror song song; không auto-mutate (`OMNI_AUTO_EXECUTE_ENABLED=false`).
3. Soát Loki sau 24–48h; so khớp với ticket incident.
