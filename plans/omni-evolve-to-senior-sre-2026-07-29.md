# Plan: Omni → Autonomous SRE phục vụ N khách hàng, hành xử như Senior SRE

> Viết 2026-07-29, sau khi đóng 3 gap top của audit 18-domain (RBAC cluster-admin, RAG
> mismatch, HITL proof). Mọi con số dưới đây **đo trực tiếp trên cluster lab hôm nay**,
> không suy diễn từ tài liệu cũ.
>
> Plan này **không thay thế** `omni-universal-sre-discovery-qwen3.6-27b-2026-07-28.md`
> (P0-P5: đổi model + universal discovery). Plan kia trả lời "làm sao khảo sát được hệ
> thống ngoài K8s". Plan này trả lời câu rộng hơn: "làm sao Omni **giỏi lên theo thời
> gian** và **nhân bản ra N khách hàng**". Hai plan giao nhau ở P2/P3/P4 — ghi rõ ở §5.

## 1. Định nghĩa "Senior SRE" — dùng làm thước đo, không nói chung chung

Một senior SRE khác một script tự động ở 5 điểm. Đây là 5 trục chấm điểm của plan này:

| # | Năng lực senior | Biểu hiện kiểm chứng được |
|---|---|---|
| S1 | **Hiểu hệ thống** | Vẽ được topology + phụ thuộc mà không cần hỏi lại; biết cái gì mình CHƯA biết |
| S2 | **Học từ sự cố** | Sự cố lần 2 xử lý nhanh/chắc hơn lần 1; kinh nghiệm thành quy trình |
| S3 | **Phán đoán rủi ro** | Biết khi nào tự làm, khi nào hỏi người; ước lượng blast radius trước khi chạm |
| S4 | **Giao tiếp** | Cảnh báo có ngữ cảnh, không spam; báo cáo đọc được bởi người không rành kỹ thuật |
| S5 | **Nhìn trước** | Đề xuất scale/capacity TRƯỚC khi vỡ, dựa trên xu hướng chứ không phải ngưỡng tĩnh |

## 2. Trạng thái thật hôm nay theo 5 trục

| Trục | Đã có (bằng chứng runtime) | Thiếu |
|---|---|---|
| **S1 Hiểu** | System Twin sống: `infra_topology` **1482 doc**; discovery 3 VM thật; `tenant_readiness_state` có **3 tenant** (`default`, `tenant-replay-01`, `staging-sim`) | Twin chỉ phủ K8s + 3 VM lab; không có API-surface/app-layer (đó là P2 của plan kia). Chưa có khái niệm "điều tôi chưa biết" ở dạng máy đọc được |
| **S2 Học** | `action_experience` **420 doc**; `itops_sop_ledger` **1093 doc** (vừa backfill hôm nay); RAG recall thật 0.878 | **`omni_admin.playbook_graduation` = 0 hàng. `playbooks` collection chỉ 2 doc.** Kinh nghiệm nằm chết ở vector store, KHÔNG bao giờ tốt nghiệp thành playbook có thẩm quyền → **vòng học đang HỞ** |
| **S3 Phán đoán** | `tier_gate` + `resolve_tier` không có đường bypass; kill-switch `false`; CRAT fail-closed ký Ed25519 thật (2505 block); blast_radius module tồn tại | **`omni_admin.hitl_decision` = 0 hàng** — chưa từng có quyết định người nào được ghi nhận → chưa có dữ liệu để hiệu chỉnh "khi nào nên hỏi người". Tier đang kẹt `shadow` cho MỌI tenant |
| **S4 Giao tiếp** | Telegram advisory VI 6 mục; advisory-ack durable (Kafka+CRAT+PG) từ vòng 2 | Không có báo cáo định kỳ (grep `weekly_report|generate_report` → không có module thật). Tenant-portal chỉ có Twin tóm tắt + incident list trơ (audit #17, 50%) |
| **S5 Nhìn trước** | `ForecastTimeline` 5 horizons trong advisory schema; forecast loop đã hết lỗi Prometheus sau vòng 2 | Không có capacity/rightsizing engine thật — grep `rightsizing|scale_recommend` chỉ ra file rời rạc, không có đường đi hoàn chỉnh từ xu hướng → khuyến nghị scale |

## 3. Chẩn đoán gốc — thứ tự này quan trọng

Ba nút thắt, **giải sai thứ tự sẽ lãng phí**:

### Nút 1 (gốc rễ): vòng học HỞ — `playbook_graduation = 0`
Omni hiện **không giỏi lên**. Nó xử lý sự cố lần thứ 100 y hệt lần đầu: RAG recall → LLM suy
luận lại từ đầu. `action_experience` 420 doc chỉ là bộ nhớ thụ động — không có cơ chế nào
biến "cách xử lý này đã đúng 5/5 lần" thành playbook có thẩm quyền, được tin tưởng, bỏ qua LLM.
Bảng `playbook_graduation` + `playbook_graduation_history` đã tồn tại trong schema (migration
đã chạy) nhưng **chưa từng có 1 hàng** → cơ chế đã thiết kế mà chưa nối dây.

**Đây là nút phải mở đầu tiên.** Mọi thứ khác (N tenant, báo cáo, capacity) đều nhân bản
năng lực hiện có; nếu năng lực đó không tự cải thiện thì nhân ra N khách hàng chỉ nhân lên
chi phí LLM, không nhân lên giá trị.

### Nút 2: không có dữ liệu hiệu chỉnh phán đoán — `hitl_decision = 0`
Tier kẹt ở `shadow` là **đúng và an toàn**, nhưng nó kẹt vì không có bằng chứng nào để nâng.
Muốn lên `assisted`/`autonomous` cho một tenant cần trả lời được: "trong N advisory vừa qua,
bao nhiêu % người duyệt đồng ý?". Hiện `omni:kpi:z:accepted`/`false_positive` **rỗng** (audit
#9) và `hitl_decision` rỗng → **không thể trả lời** → không bao giờ nâng tier được một cách có
căn cứ. Vòng 2 đã xây advisory-ack ghi PG; giờ cần **dùng** dữ liệu đó làm thước đo.

### Nút 3: onboarding tenant thứ N chưa chứng minh tự phục vụ
3 tenant trong `tenant_readiness_state` nhưng đều do lab tạo. Chưa có bằng chứng một tenant
mới đi hết: provision → enroll agent → discovery → Twin → advisory đầu tiên **mà không cần
kỹ sư can thiệp thủ công**. Post-mortem `drift-correction-2026-07-02.md` đã ghi đúng cái bẫy:
thiếu `create_tenant()` gây FK violation liên tục. Đây là nút chặn "N khách hàng".

## 4. Lộ trình — 4 giai đoạn, mỗi giai đoạn có tiêu chí đóng đo được

Nguyên tắc xuyên suốt: **không nới lỏng invariant nào** (kill-switch, CRAT fail-closed,
INV_NAMESPACE_ISOLATION, executor never cluster-admin, mutations only via executor).

### G1 — ĐÓNG VÒNG HỌC (mở nút 1). Ưu tiên cao nhất.
Nối `action_experience` → `playbook_graduation` → `playbooks` collection.
- Định nghĩa điều kiện tốt nghiệp: cùng symptom-class, cùng tool, **N lần liên tiếp
  verified thành công**, không có rollback → promote thành playbook có `confidence` và
  `graduated_at`. Ngưỡng N phải là config, không hardcode.
- Playbook đã tốt nghiệp được `recall_playbook_advisory()` ưu tiên trước LLM (instinct dự án
  đã ghi: recall ≥ 0.75 thì skip LLM) → giảm chi phí LLM **và** tăng tính nhất quán.
- Hạ cấp tự động: playbook thất bại → mất bậc, không im lặng.
- **Tiêu chí đóng**: `select count(*) from omni_admin.playbook_graduation` > 0 với hàng đến
  từ sự cố thật (không phải seed), và chứng minh được 1 trace đi qua playbook thay vì LLM.

### G2 — HIỆU CHỈNH PHÁN ĐOÁN (mở nút 2). Sau G1.
- Nối advisory-ack (đã có từ vòng 2) vào `omni:kpi:z:accepted` / `false_positive` để 2 KPI
  này hết rỗng → 7 alert rule Prometheus hết vô nghĩa (audit #9).
- Ghi `hitl_decision` mỗi lần người duyệt/từ chối qua Telegram.
- Xây **tiêu chí nâng tier có căn cứ**: chỉ đề xuất `shadow → assisted` cho một tenant khi
  acceptance-rate ≥ ngưỡng trên ≥ N advisory trong ≥ M ngày. Việc nâng vẫn do **người** bấm,
  Omni chỉ được **đề xuất kèm bằng chứng** — không tự nâng quyền cho chính mình.
- **Tiêu chí đóng**: dashboard/API trả được "tenant X: acceptance 82% trên 45 advisory/14
  ngày → đủ điều kiện đề xuất assisted", số liệu từ PG thật.

### G3 — NHÂN BẢN RA N KHÁCH HÀNG (mở nút 3). Song song được với G2.
- Kịch bản "tenant thứ N tự phục vụ": một lệnh/endpoint duy nhất tạo tenant → phát
  enroll-token → agent tự đăng ký → discovery → Twin có dữ liệu → advisory đầu tiên.
  Chạy thật với 1 tenant MỚI hoàn toàn, không dùng lại 3 tenant lab.
- Kiểm chứng cách ly: tenant A không recall được `action_experience`/SOP của tenant B
  (đã có `tenant_id` xuyên suốt — cần **test đối kháng**, không chỉ tin thiết kế).
- Đo chi phí biên: N tenant chạy song song thì Ollama chịu được bao nhiêu (đây chính là P0
  của plan Qwen3.6:27b — **gộp vào đây, đừng benchmark 2 lần**).
- **Tiêu chí đóng**: tenant thứ 4 đi hết vòng mà không có thao tác tay nào ngoài lệnh khởi tạo;
  test đối kháng cross-tenant recall trả về rỗng.

### G4 — NĂNG LỰC SENIOR CÒN THIẾU (S4/S5). Sau cùng.
- **Báo cáo định kỳ**: tái dùng đúng cấu trúc 10 mục của báo cáo mẫu FPT Loyalty, nguồn dữ
  liệu là Knowledge Graph + confidence (đây là P5 của plan kia). Đích đến là tenant-portal,
  không chỉ provider-portal — vá đúng gap audit #17.
- **Capacity/scale advisory**: từ baseline 3σ + forecast đã có → khuyến nghị rightsizing kèm
  bằng chứng xu hướng. Không tự scale, chỉ đề xuất (mutations only via executor + tier gate).
- **Khám phá ngoài K8s**: P2/P3/P4 của plan Qwen3.6:27b.

## 5. Quan hệ với plan Qwen3.6:27b (tránh làm trùng)

| Plan kia | Vị trí trong plan này |
|---|---|
| P0 benchmark model | **Gộp vào G3** (đo throughput N tenant song song — cùng một phép đo) |
| P1 đổi model theo role | Độc lập, chạy lúc nào cũng được sau P0 |
| P2 API-surface collector | Thuộc **G4** (mở rộng S1) |
| P3 Fact.confidence + `same_codebase_as` | Thuộc **G4**, nhưng `confidence` cũng phục vụ G1 → cân nhắc kéo sớm |
| P4 tổng quát hoá ReAct discovery | Thuộc **G4** |
| P5 export báo cáo | Thuộc **G4** |

**Kết luận về thứ tự**: plan Qwen3.6:27b toàn bộ nằm ở G3/G4. Nếu chạy nó TRƯỚC G1/G2 thì
Omni sẽ khảo sát được nhiều hệ thống hơn, bằng model to hơn, nhưng **vẫn không giỏi lên** và
**vẫn kẹt tier shadow**. Đó là mở rộng bề ngang trên một cái nền chưa tự cải thiện.

## 6. Việc KHÔNG làm

- Không nâng tier tự động dựa trên chỉ số do chính Omni sinh ra mà không có người duyệt.
- Không hồi sinh `omni-hitl-dispatcher` Kafka (quyết định đã chốt; HITL chạy qua Telegram —
  đã proof E2E PASS 2026-07-29).
- Không hardcode chi tiết riêng của một khách hàng vào code — mọi thứ qua `tenant_id`.
- Không để playbook tốt nghiệp bỏ qua CRAT hoặc tier gate; "tin tưởng hơn" ≠ "ít kiểm soát hơn".

## 7. Next step

G1 là nút gốc. Cần user duyệt trước khi code:
1. Audit `src/services/learning_promoter/promoter.py` (đã tồn tại, có ghi `COLLECTION_SOP`)
   để xem cơ chế promote đã có tới đâu, thiếu đoạn nào tới `playbook_graduation`.
2. Xác định ngưỡng tốt nghiệp cùng user (N lần thành công liên tiếp là bao nhiêu).
3. Viết test trước theo TDD, rồi nối dây, rồi chứng minh bằng 1 trace thật đi qua playbook.
