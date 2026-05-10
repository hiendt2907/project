# Demo bốn luồng + template Telegram (Omni + Smart SIEM)

Tài liệu tham chiếu: thống nhất **suggest + ticket**, **không mutate**; **Smart SIEM bắt buộc**; lane 3 giai đoạn 1 = **HTTP/API & log** (không ontology nghiệp vụ sâu trừ khi có KPI riêng).

**Thành công demo:** một tin Telegram đủ **What, When, Who, Why, How to** (+ Refs, Risk, Next check).

---

## Template Telegram chung

Mỗi tin **một block**, field cố định (không bỏ trống im lặng; nếu không biết ghi `unknown` + lý do):

```
🔔 <SHORT_TITLE> | <STREAM_TAG>
━━━━━━━━━━━━━━━━
**What:** <1 câu: hiện tượng + tác động>
**When:** <timezone-aware, ví dụ 2026-05-05T14:32:07+07 / duration nếu là surge>
**Who:** <workload/service/tenant/namespace/host/db — càng cụ thể càng tốt>
**Why:** <1–3 bullets: bằng chứng đã có — metric/log/state/SIEM ref>
**How to:** <3–7 bước read-only + ticket; không mutate>
━━━━━━━━━━━━━━━━
Refs: <trace_id / incident_id / link log query / dashboard>
Risk: <low|med|high> | Next check: <thời điểm hoặc PromQL/log filter>
```

**`<STREAM_TAG>`:** `SYS_RESOURCE` | `SYS_HARD_FAIL` | `APP_HTTP` | `SIEM_SECURITY`

---

## Demo 1 — System resource (`SYS_RESOURCE`)

| Mục | Nội dung |
|-----|----------|
| **Kích hoạt** | CPU hoặc mem workload vượt ngưỡng / z-score (lab: stress 5–10 phút). |
| **Nguồn chứng cứ** | Metric time series + (tuỳ môi trường) baseline / temporal block. |
| **Kỳ vọng What** | Workload X tài nguyên cao bất thường; nguy cơ latency/queue — không kết luận root cause nếu chưa đủ lane khác. |
| **When** | Đỉnh + khoảng surge; so với baseline. |
| **Who** | `namespace/deployment` hoặc VM `host/service`. |
| **Why** | Bullet chỉ metric (p95 CPU, z nếu có); không đoán business. |
| **How to** | Đọc top proc / thread dump read-only; dashboard; ticket: xác minh traffic vs leak; **không restart auto**. |

---

## Demo 2 — Hard fail / hệ thống (`SYS_HARD_FAIL`)

| Mục | Nội dung |
|-----|----------|
| **Kích hoạt** | CrashLoopBackOff / mount fail / DB refuse / NotReady (lab: bad image hoặc wrong volume). |
| **Nguồn chứng cứ** | **Đa luồng:** `state` (K8s events) + `app_log` + metric nếu có. |
| **Kỳ vọng What** | Một câu đúng phạm vi: container exit / mount / readiness — workload không ổn định. |
| **When** | FirstSeen / LastTransition + số lần restart. |
| **Who** | Pod, node pool, PV/PVC, DB endpoint. |
| **Why** | Tối thiểu 2 bullet từ **hai lane khác nhau** (event + log line). |
| **How to** | describe / log tail read-only; checklist config; ticket kèm events + log ref. |

---

## Demo 3 — Application HTTP (`APP_HTTP`)

| Mục | Nội dung |
|-----|----------|
| **Kích hoạt** | Surge **5xx** hoặc **429** trên access log / Envoy (lab: fault inject hoặc rate limit). |
| **Nguồn chứng cứ** | Histogram status / dominant error class / sample log lines. |
| **Kỳ vọng What** | API availability degraded — server errors / rate limit; không mix lane 1 trừ khi đã chứng minh. |
| **When** | Window surge + so với baseline. |
| **Who** | Service / route / upstream (theo log). |
| **Why** | Status breakdown + 1–2 dòng log (anonymized nếu cần). |
| **How to** | Filter log `status/route`; kiểm dependency; ticket: request rate + error rate; không scale/restart auto. |

---

## Demo 4 — SIEM / Smart SIEM (`SIEM_SECURITY`)

| Mục | Nội dung |
|-----|----------|
| **Kích hoạt** | Incident qua Redis/Kafka path vào pipeline Omni (category + severity từ SIEM). |
| **Nguồn chứng cứ** | Incident envelope + evidence raw + correlation id. |
| **Kỳ vọng What** | Category ngắn + impact một câu **theo dữ liệu SIEM**. |
| **When** | `first_seen` / detection time. |
| **Who** | Tenant / source IP / host / principal (theo policy dữ liệu). |
| **Why** | Rule name + 1–2 IOC/telemetry refs (**không** bịa). |
| **How to** | Điều tra read-only (session, IAM, log retention…) + ticket SOC; không block IP auto nếu policy forbid. |

---

## Checklist QA sau mỗi demo

- [ ] Đủ **What / When / Who / Why / How to**.
- [ ] `STREAM_TAG` đúng luồng.
- [ ] **Why** có ít nhất một ý trích từ nguồn (metric/log/state/SIEM).
- [ ] **How to** chỉ read-only + ticket; không mutate.
- [ ] Có **Refs** (trace/incident/dashboard/log query).
- [ ] **Risk** và **Next check** hợp lý.

---

## Payload ticket (copy từ Telegram)

```
Title: [<STREAM_TAG>] <SHORT_TITLE>
Priority: <P1-P4>
Scope: <Who>
Evidence summary: <Why bullets>
Steps taken (read-only): <How to tóm tắt>
Links: <Refs>
```

---

## Bối cảnh ICP (ghi nhớ ngắn)

- Enterprise, hệ thống tách rời + internal platform.
- Giá trị: giảm Opex, giảm rủi ro, báo cáo & dự đoán; **trách nhiệm khi AI sai: người / HITL / vận hành** (không auto-mutate).
- Runtime ưu tiên demo: **K8s, bare metal VM, DB**.
