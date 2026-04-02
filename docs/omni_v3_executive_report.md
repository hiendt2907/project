---
marp: true
theme: default
class: invert 
---
# Slide 1: Tầm nhìn AI-SRE Tự Trị (Omni-Worker V3)

- Chuyển dịch từ **phản ứng thủ công** sang **vòng lặp tự trị** trên Kubernetes: quan sát baseline, suy luận có trạng thái, hành động có kiểm soát.
- **Một Omni-Worker** tập trung điều phối inference (Ollama) với giới hạn ngữ cảnh **4096 token** và cơ chế slot — giảm tải cognitive cho SRE.
- Định vị: **lớp điều phối an toàn** giữa Prometheus/baseline và thao tác cluster; **không** thay thế toàn bộ runbook mà **tăng tốc** chẩn đoán và khắc phục.
- Tham chiếu kỹ thuật đầy đủ: `docs/omni_v3_architecture_whitepaper.md`.
---
# Slide 2: Thực trạng và Giải pháp (Không nhầm HPA với “hiện trạng triển khai”)

- **Thực trạng** (lý do dự án ra đời): vận hành vẫn **phản ứng thủ công** trên luồng cảnh báo — SRE lần theo từng metric, khó tương quan triệu chứng, dễ **mệt cảnh báo** và can thiệp muộn; co giãn hay chỉnh rollout **không tự giải thích** gốc rễ (latency, drift theo giờ, false positive theo mùa). Omni V3 nhằm **thay thế cách làm đó** bằng vòng lặp tự trị có quan sát baseline và kiểm soát hành động.
- **HPA** (và autoscaler tương tự) chỉ là **tính năng** Kubernetes: phản ứng CPU/RAM (và tín hiệu tùy cấu hình) để **scale** — hữu ích cho co giãn, **không** đồng nghĩa với “đã có AI-SRE” và **không** thay cho tương quan đa biến trước khi can thiệp.
- **Omni V3** dùng **tín hiệu đa biến** (CHS, baseline, sự kiện) và **ReAct có trạng thái** để suy luận có ngữ cảnh — hướng **Predictive Healing**, giảm rủi ro “bắn nhầm” rollout so với chỉ dựa một ngưỡng đơn lẻ.
- Giá trị kinh doanh: **ổn định dịch vụ**, **giảm rủi ro thay đổi sai** khi quyết định dựa trên hồ sơ triệu chứng và policy, không chỉ trên một chỉ số co giãn.
---
# Slide 3: Luồng Dữ liệu Tổng thể (Prometheus → Redis → Decider → Tool → Loki)

- **Recording rules** Prometheus (baseline + seasonal) được worker đọc qua vòng **baseline snapshot**; kết quả ghi manifest vào Redis key **`omni:baseline:snapshot`**.
- **Autonomous decider** đọc snapshot, tính fingerprint, ghép prompt và (nếu có) prior state; gọi **Ollama** rồi **ToolRegistry** thực thi tool an toàn.
- Chuỗi suy luận và tool có thể ghi **một dòng JSON** qua `log_react_json` — **Loki** (Promtail) thu thập để audit (chi tiết luồng: whitepaper §2).
- Sơ đồ Mermaid đầy đủ nằm trong whitepaper §2 — báo cáo này giữ mức tóm tắt điều hành.
---
# Slide 4: Stateful ReAct — Đầu vào LLM và Redis

- Decider chỉ chạy tick ReAct khi manifest có **`dr`** hoặc **`evt`** không rỗng; đọc snapshot từ Redis (`REDIS_KEY_SNAPSHOT` trong `baseline_snapshot.py`).
- **Fingerprint** `fp` bám theo các trường manifest (ví dụ `dr`, `evt`, `z_cpu`, `z_mem`) — khóa **cooldown** và **react state** theo cùng `fp`.
- Redis: **`omni:autonomous:react_state:{fp}`** lưu JSON tóm tắt sau observation; **`omni:autonomous_fix:cooldown:{fp}`** sau CLEAR/abort/tick.
- Payload state tiêu biểu: `turn`, `last_tool`, `obs` (đã mask/cắt), `ts` — TTL theo cấu hình (`react_state_redis_ttl_sec`).
---
# Slide 5: So sánh “Lần trước” và “Lần này” (Đúng theo code)

- Hệ thống **không** thực hiện diff số học tự động giữa hai snapshot Prometheus trong decider.
- Cơ chế thực tế: tick sau nhận **manifest mới** từ Redis + (nếu tồn tại key) chuỗi **`Prior ReAct state (Redis, same fp):`** — JSON state kỳ trước nối vào **user message**.
- LLM tự tương quan ngữ cảnh; tránh kỳ vọng “engine so sánh z-score hai phiên” không có trong mã nguồn.
---
# Slide 6: Bộ nhớ và “Tự học” Ba Lớp (A / B / C — Không fine-tune)

- **Không** cập nhật trọng số mô hình Ollama; “tự học” ở đây là **bộ nhớ có cấu trúc + retrieval** và trạng thái phiên.
- **A — Stateful ReAct:** prior state trên Redis theo `fp` (slide 4–5).
- **B — Deep Scout:** embed topology → Qdrant **`infra_topology`**; map tên→namespace → Redis **`infra:learned:*`** (TTL `learned_map_ttl_sec`).
- **C — Routing:** sau slow-path thành công, **upsert Qdrant `action_experience`**; request sau có thể **fast-path** nếu đủ điểm similarity và `routing_source` hợp lệ.
---
# Slide 7: Plugin Registry và JSON Schema (Pydantic v2)

- Mỗi tool đăng ký bằng **`@register_tool`** với **Pydantic input model**; `ToolRegistry.invoke` gọi **`model_validate`** trước handler — contract thống nhất.
- **`model_json_schema()`** (Pydantic v2) được cắt lát đưa vào system prompt (**`_schemas_for_safe_tools`** trong decider) — LLM thấy schema đúng kiểu OpenAPI/JSON Schema.
- Lợi ích mở rộng domain (DB, Network, Security): thêm tool = **model + handler + đăng ký + allowlist** — giảm “prompt drift” so với mô tả tự do.
---
# Slide 8: Chính sách Thực thi Kubernetes (SDK-only, Async)

- Thao tác cluster qua **`kubernetes_asyncio`** (describe, logs, scale, patch, …) — **không** subprocess shell tùy ý; phù hợp policy **K8s Python SDK** trong kiến trúc.
- RBAC tập trung trên ServiceAccount worker — kiểm soát phạm vi namespace/tài nguyên theo môi trường.
- Giảm bề mặt tấn công và sai lệch giữa môi trường so với chạy lệnh thô trên pod.
---
# Slide 9: Đa biến — Composite Health Score (CHS)

- **CHS** = tổng có trọng số trên các **|z|*** (cpu, mem, disk, net…) — trọng số từ **`OMNI_CHS_WEIGHTS`** (JSON trong cấu hình).
- Ngưỡng **`chs_threshold`** đưa vào manifest; cờ **`wide_incident`** khi CHS vượt ngưỡng — hỗ trợ ưu tiên làm rộng sự cố đa chiều.
- Thay vì báo động từng metric lẻ, CHS gom tín hiệu — hướng tới **giảm fatigue vận hành** và tập trung incident thực chất.
---
# Slide 10: Đa biến — Seasonal Drift (Week-over-Week)

- Recording rules Prometheus so sánh **cùng khung giờ với tuần trước** (ví dụ trục CPU seasonal) để giảm **false positive** do pattern cố định theo giờ.
- Thiết kế có **fallback**: ví dụ biểu thức seasonal kết hợp `or omni:node_cpu:z` khi chưa đủ lịch sử 7 ngày — tránh chuỗi rỗng.
- Worker ghi **`seasonal_drift_z`** vào manifest khi cấu hình PromQL seasonal — đồng bộ với decider/baseline (whitepaper §6).
---
# Slide 11: Bảo mật — Policy-as-Code (Guardrail Scale)

- Ví dụ **`k8s_scale_deployment`**: trường **`replicas`** giới hạn **0..10** tại tầng Pydantic — từ chối **trước** khi gọi API Kubernetes.
- **`ToolRegistry.invoke`** luôn validate model trước handler — lớp **Policy-as-Code** độc lập với LLM.
- Nghiệm thu: `replicas=11` hoặc `replicas=-1` → **`ValidationError`** (không tới `AppsV1Api`).
---
# Slide 12: Bảo mật — Data Sanitization (JWT, Password, PEM)

- Pipeline observation: cắt độ dài rồi **`sanitize_for_llm`** — regex theo thứ tự: credential dạng key=value, **Bearer** token, khối **PEM**.
- Minh họa **TRƯỚC:** chuỗi chứa `Bearer eyJ…` dài và `password=SuperSecret123!`.
- Minh họa **SAU:** chứa **`[REDACTED]`** / `Bearer [REDACTED]` — giảm rò rỉ bí mật vào prompt và kênh thông báo.
---
# Slide 13: Vận hành An toàn — Deadlock Breaker (Max Turns)

- Giới hạn **`OMNI_REACT_MAX_TURNS`** / `react_max_turns` — vòng ReAct kết thúc khi resolved hoặc hết turn.
- Khi **không** resolved sau tối đa turn: log lỗi có **`[REACT_ABORTED]`**, đồng thời **`log_react_json("v3_react_aborted", …)`**; có thể cảnh báo Telegram nếu bật.
- Tránh worker kẹt vô hạn trong suy luận — yêu cầu vận hành có kiểm soát.
---
# Slide 14: Zero-Trust — Human-in-the-Loop (Approval)

- Luồng **`request_approval`**: token Redis + xác nhận qua **Telegram admin** (cấu hình chat) — mặc định **không** thực hiện thao tác nhạy cảm khi chưa duyệt.
- Phù hợp mở rộng **mutate / patch** production — lớp **Zero-Trust** bổ sung cho Policy-as-Code.
- Roadmap: siết audit trail và policy theo **lab vs production**.
---
# Slide 15: Quan sát và Kiểm toán (Loki, reasoning_path)

- **`log_react_json`** xuất **một dòng** JSON compact (`separators=(",", ":")`) — dễ grep và parse trên Loki.
- Trường **`reasoning_path`** (ví dụ `v3_react_thought`, `v3_tool_execute`, `v3_react_aborted`) phục vụ **phân loại** và audit sau sự cố.
- Liên kết với stack giám sát hiện có (Prometheus + Loki) — không thay thế hồ sơ compliance ngân hàng chính thức.
---
# Slide 16: Nghiệm thu Kỹ thuật (pytest và Chốt chặn)

- **pytest toàn bộ `tests/`**: **255** test **PASS**, **0** FAIL (số liệu chạy tự động trong chuỗi nghiệm thu).
- **Đã kiểm chứng:** `registry.invoke` + `k8s_scale_deployment` với `replicas=11` / `replicas=-1` → `ValidationError` trước Kubernetes; sanitize JWT + password → chứa `[REDACTED]`; `log_react_json` → `json.loads` OK với `"reasoning_path": "v3_react_thought"`.
- **Khuyến nghị Golive:** bộ kiểm thử xanh — triển khai **có kiểm soát**, kèm giám sát Loki/Prometheus và rà soát RBAC theo checklist vận hành.
---
# Slide 17: Roadmap và Quản trị Rủi ro

- Chuẩn hóa **nhãn agent** (DB / Net / Sec / Core) và **phạm vi tool** theo domain — tránh can thiệp chéo khi mở rộng.
- Roadmap: suite K8s sâu hơn (ví dụ audit API server), dashboard Grafana biến thể, **multi-agent orchestration** khi tách SRE theo miền.
- **Giới hạn tài liệu:** báo cáo slide và whitepaper mô tả **kiến trúc kỹ thuật nội bộ** — **không** thay thế hồ sơ tuân thủ ngân hàng đã phê duyệt; mọi Golive production cần sign-off theo quy trình tổ chức.
