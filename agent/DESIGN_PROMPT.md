# Onboarding + Operations Agent — Design Prompt (chốt 2026-06-19)

> Nguồn: hội thoại prompt-optimizer 2026-06-19. Đây là bản blueprint-kickoff prompt cuối cùng
> sau 4 vòng làm rõ. Dùng làm input cho `blueprint` skill khi bắt đầu implement trong thư mục
> `agent/`.

## Prompt

Use blueprint skill để lập kế hoạch: "Onboarding + Operations Agent đa-tenant cho hệ thống
khách hàng, mở rộng trên nền Remote Agent Sensor Model + Lane 1-4/Advisory Mode đã có trong Omni."

### BỐI CẢNH ĐÃ CHỐT QUA THẢO LUẬN (không tự suy diễn lại)

- Target = hệ thống của KHÁCH HÀNG, nhiều tenant (1→100), mỗi tenant cách ly dữ liệu với nhau.
- Ranh giới dữ liệu: agent deploy trên máy khách ĐƯỢC PHÉP đọc và gửi raw logs + metric về
  Omni trung tâm để vận hành (không phải zero-egress tuyệt đối).
- Vận hành KHÔNG xây lại từ đầu — tái dùng Lane 1-4 + AnalystAdvisory + Advisory Mode
  (SUGGEST_REMEDIATION) + unified_incident_card.py (Telegram), chạy trên data tenant đó gửi về,
  cách ly bằng tenant_id ở mọi tầng (Redis key, RAG/SOP, baseline 3σ, audit chain CRAT, Telegram
  chat_id) — mở rộng pattern OMNI_TENANT_APIKEYS/KPI-per-tenant đã có sang RAG/SOP/baseline
  (hiện `omni:rag:sop` đang gộp chung mọi tenant, đây là gap phải vá trước).
- Sandbox giả lập lỗi hiếm CHỈ mở sau khi onboarding đạt ngưỡng đủ kiến thức (readiness gate).
- Diagram: lưu MÃ MERMAID THÔ làm nguồn chuẩn (text, versioned, cách ly theo tenant) — KHÔNG
  render ra ảnh để lưu trữ. UI endpoint render trực tiếp bằng mermaid.js phía client. Chỉ render
  PNG on-demand, không persist, khi cần nhúng vào nơi không chạy JS (vd Telegram digest ở B4).

### NHÓM A — ONBOARDING (tự học hệ thống khách hàng, mỗi tenant độc lập)

**A1. Tìm hiểu hệ thống, kiến trúc, nghiệp vụ**
- Remote agent (mở rộng từ `project_remote_agent_sensor_model`) tự khám phá tại máy khách:
  process list, port mở, service topology, dependency graph cơ bản (DB/queue/cache nào đang
  chạy), tech stack (ngôn ngữ, framework qua file manifest nếu agent có quyền đọc).
- Đây là bước ƯU TIÊN 1 — agent tự làm trước, không chờ khách hàng cung cấp.

**A2. Đọc tài liệu hệ thống, API, nghiệp vụ**
- Agent đọc các nguồn có sẵn tại máy khách: README, OpenAPI/Swagger spec, file cấu hình,
  comment trong code, log mẫu (để suy ra luồng request thật).
- Gửi raw content + metadata trích xuất về Omni trung tâm (theo tenant_id), không xử lý LLM
  tại máy khách (giữ agent nhẹ, đúng yêu cầu "chỉ deploy 1 con agent").

**A3. Tổng hợp tài liệu**
- Worker mới tại Omni trung tâm (role=onboarding, tiêu thụ "discovery evidence" — loại evidence
  mới song song với state/app_log/metrics hiện có) tổng hợp dần thành 1 bộ tài liệu sống theo
  tenant: kiến trúc, danh sách API, luồng nghiệp vụ suy ra được.
- Lưu theo namespace tenant riêng (không trộn — giống nguyên tắc cách ly RAG/SOP).

**A4. Vẽ diagram kiến trúc hệ thống, kiến trúc API, logic nghiệp vụ**
- Sinh MÃ MERMAID (kiến trúc thành phần, sequence API chính, flowchart nghiệp vụ) từ dữ liệu
  tổng hợp A3 — lưu text này làm NGUỒN CHUẨN theo tenant (Redis/DB, versioned, diff được mỗi
  lần regenerate khi có evidence mới).
- KHÔNG render ảnh để lưu trữ. UI endpoint riêng cho tenant nhận mã Mermaid thô và render bằng
  mermaid.js phía client (nhẹ, không cần render-service, luôn khớp bản mới nhất).
- Khi cần xuất ra nơi không chạy JS (Telegram digest ở B4, báo cáo PDF nếu có sau này) → render
  PNG on-demand lúc gửi (mermaid-cli hoặc tương đương), không persist ảnh đó.

**A5. Tài liệu không đủ → hỏi, hoặc đọc biên bản bàn giao**
- Khi worker onboarding phát hiện gap (vd: 1 API không rõ mục đích, 1 luồng nghiệp vụ thiếu
  bước xác nhận), tự sinh câu hỏi cụ thể → gửi cho admin của TENANT đó qua kênh Telegram
  per-tenant (tái dùng cơ chế Telegram hiện có, chat_id riêng theo tenant).
- Nếu khách hàng có cung cấp "biên bản bàn giao" (tài liệu handover, post-mortem cũ, v.v.) thì
  nạp vào làm nguồn bổ sung cho A2/A3 — tương đương 1 loại evidence input khác.
- LẶP LẠI A1→A5 (quét thêm + hỏi thêm) cho tới khi đạt CHECKLIST ĐỦ — định nghĩa cụ thể (ví dụ:
  % service đã map endpoint, % luồng nghiệp vụ chính được xác nhận qua thực tế, 0 câu hỏi mở
  chưa trả lời quá X ngày) — không phải vòng lặp vô hạn hay số lần cố định.
- Khi checklist đạt ngưỡng → set READINESS FLAG cho tenant đó, mở khóa Nhóm B + Nhóm C.

### NHÓM B — VẬN HÀNH (chỉ mở sau khi tenant qua readiness gate A5)

**B1. Theo dõi hệ thống**
- Reuse Lane 1-4 nguyên vẹn (3σ baseline `3sigma:remote:` theo tenant, resource/app_log/
  metrics/SIEM) chạy trên data agent gửi về — baseline KHÔNG chia sẻ giữa tenant.

**B2. Truy vết và xử lý lỗi (bước đầu CHỈ truy vết)**
- Map thẳng vào Advisory Mode hiện có: AnalystAdvisory (WHAT/WHO/WHY/HOW-TO) +
  SUGGEST_REMEDIATION — TUYỆT ĐỐI không EXECUTE_MUTATE cho tới khi tenant đó được nâng tier
  rõ ràng (đúng nguyên tắc kill-switch fail-closed của Omni).

**B3. Giả lập lại các lỗi hiếm gặp và không ai xử lý được trước đó**
- CHỈ chạy sau khi readiness gate A5 pass. Dùng chaos harness có sẵn
  (omni_dev_death_loop.sh / chaos-drill-rollback) làm runner.
- Input lỗi-hiếm lấy từ RAG/post-mortem RIÊNG của tenant đó (không trộn case giữa tenant).
- Output (case đã giả lập + cách xử lý xác nhận) ghi ngược vào RAG riêng tenant → tăng dần
  kiến thức vận hành cho lần truy vết thật sau.

**B4. Thông báo cho system admin (con người) biết vấn đề đang hiện hữu (baseline)**
- Tái dùng unified_incident_card.py gửi Telegram theo chat_id riêng tenant. Nếu digest cần
  kèm diagram, render PNG on-demand từ mã Mermaid A4 lúc gửi (không lưu ảnh).
- Cần thêm 1 digest định kỳ tổng hợp baseline ngoài card alert tức thời — quyết định cụ thể
  ở /plan.

**B5. Vận hành và giám sát liên tục không ngừng nghỉ**
- Chạy như 1 worker role mới theo tenant (tham khảo pattern OMNI_WORKER_ROLE hiện có, không
  phải script chạy 1 lần) — đảm bảo tự phục hồi khi pod restart (giống cơ chế
  auto_offset_reset="earliest" hiện có cho kafka_evidence_loop).

### PHASE THỰC THI (để blueprint chia PR)

1. **Phase 1**: Tenant-isolation cho RAG/SOP/baseline (vá gap `omni:rag:sop` đang gộp chung)
2. **Phase 2**: Remote agent — mở rộng raw-log forwarding + discovery evidence (A1, A2)
3. **Phase 3**: Onboarding worker tại Omni — tổng hợp (A3) + sinh & lưu mã Mermaid (A4) + ask-loop (A5)
4. **Phase 4**: Readiness gate — checklist đủ → mở Lane 1-4/Advisory cho tenant (B1, B2)
5. **Phase 5**: Sandbox chaos theo tenant (B3) + digest baseline cho admin (B4) + wiring continuous worker role (B5)

Mỗi phase = 1 PR, `/verify` gồm test xác nhận KHÔNG leak chéo tenant (RAG, baseline, audit
chain, mã Mermaid). Tier/kill-switch giữ shadow cho tenant tới khi qua Phase 4. python-reviewer
+ code-reviewer review sau mỗi phase. Model: Sonnet 4.6 hầu hết phase, Opus 4.8 chỉ cho Phase 1
(thiết kế namespace cách ly).

### KHÔNG LÀM

- Không viết lại Lane 1-4/AnalystAdvisory/CRAT/unified_incident_card từ đầu.
- Không gộp RAG/SOP/baseline/case-giả-lập/mã-Mermaid giữa các tenant.
- Không render và lưu trữ ảnh diagram — chỉ lưu mã Mermaid thô, render ảnh là tạm thời/không persist.
- Không mở B3 (sandbox) trước khi A5 readiness gate pass.
- Không cho agent tự mutate hạ tầng khách hàng ở B2 khi tier=shadow.
