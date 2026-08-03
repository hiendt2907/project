# Omni v2 — RED TEAM Round 2 (Distributed Systems + Production SRE Review)

**Vai trò:** Principal Architect / Distributed Systems Reviewer / Production SRE Reviewer — nhiệm vụ: PHÁ kế hoạch, không xác nhận nó.
**Phạm vi:** không lặp lại bất kỳ finding nào ở `OMNI_V2_RED_TEAM_REVIEW.md` (Round 1 — polarity blast-radius, landmine `teardown-omni-postgres`, overengineering `AutonomyControlPlane`, v.v.). Chỉ tìm vấn đề Round 1 **bỏ sót**.
**Phương pháp:** verify lại bằng lệnh thật trên code hiện tại (`grep`/đọc file trực tiếp), không suy đoán.

---

## Bằng chứng xác minh mới (Round 2, không trùng Round 1)

| Kiểm tra | Kết quả |
|---|---|
| `grep maxmemory k8s/deployments/*.yaml` | `redis-standalone.yaml`: `maxmemory 2gb`, **`maxmemory-policy allkeys-lru`** |
| Tên file Redis deployment | `redis-standalone.yaml` — tự đặt tên đã xác nhận không HA (không Sentinel, không Cluster) |
| `src/pkg/autonomy/tier_gate.py::resolve_tier()` đọc code thật | **KHÔNG có `try/except` quanh `await read_tier_cached(redis, tenant_id)`** — trong khi 20 dòng dưới, `_apply_plan_ceiling()` trong CÙNG FILE lại có `try/except Exception` fail-closed đầy đủ. Bất nhất ngay trong 1 file |
| `grep send_and_wait\|\.send( src/workers src/gateway/routes` | **0/9 call site nào truyền `key=`** — `hitl_dispatcher.py`, `siem_bridge.py` (2 lần), `agent_push.py`, `agent_webhook.py`, `autonomy.py`, `diagnostic.py`, `simulate.py` — tất cả gửi Kafka KHÔNG có partition key |
| `write_audit_block()` — thứ tự Producer (Đ13/WS2) | Đã xác nhận từ Round 1: (1) kill-switch (2) `write_audit_block` (3) Kafka send — **CRAT ghi TRƯỚC KHI Kafka publish được xác nhận thành công** |

---

## 1. Architectural Contradiction

**#R2-1 — "Decision" có 3 owner không đồng bộ, thứ tự ghi sai (CRAT trước Kafka)**
CRAT block (`MUTATION_ENQUEUED`), `omni:trace:stages` (DISPATCH stage), và message thật trên topic `omni-actions` đều là "bản ghi Decision" — nhưng **không có precedence nào được định nghĩa nếu chúng lệch nhau**. Nguy hiểm hơn: Producer ghi CRAT **trước** khi Kafka `send_and_wait` được xác nhận (đã verify ở Round 1 khi mô tả chuỗi WS2, nhưng Round 1 KHÔNG soi ra hệ quả consistency này). Nếu `send_and_wait` fail SAU khi CRAT đã ghi `MUTATION_ENQUEUED` → audit log nói "đã enqueue" nhưng thực tế **không có message nào được gửi** — vĩnh viễn lệch nhau, không ai phát hiện trừ khi có reconciliation sweep (không tồn tại).

## 2. Hidden Coupling

**#R2-2 — `resolve_tier()` KHÔNG fallback graceful khi Redis mất kết nối (chỉ fallback khi cache-miss)**
Docstring/tài liệu toàn phiên (CLAUDE.md, cả 3 file trước) đều mô tả "Redis cache → Postgres → env" như 1 chuỗi fallback resilient. Code thật: `await read_tier_cached(redis, tenant_id)` không có try/except bao quanh — nếu Redis timeout/connection-refused (không phải cache-miss, mà là LỖI KẾT NỐI), exception này KHÔNG được bắt trong `resolve_tier`, sẽ lan lên caller. Toàn bộ narrative "3-tier fallback" chỉ đúng cho trường hợp "Redis sống nhưng chưa có key" — SAI cho trường hợp "Redis chết". Đây là **coupling ngầm**: `tier_gate` (Autonomy) phụ thuộc cứng vào Redis còn sống dù thiết kế tuyên bố ngược lại.

**#R2-3 — Zero partition-key trên MỌI Kafka producer call site**
Xác nhận bằng grep thật: không có `key=` ở bất kỳ `send_and_wait` nào trong toàn repo. Với topic 1-partition (đa số hiện tại) không sao — nhưng `omni-knowledge-evidence` (3 partition) và các topic SIEM (6 partition) **không có cơ chế nào đảm bảo 2 sự kiện của CÙNG 1 tenant/resource cùng vào 1 partition** → không đảm bảo thứ tự xử lý giữa chúng. Đây là hidden coupling ngược: mọi logic downstream (SIEM correlation sequence-score, Change Ledger "gần đây có gì đổi" theo WS8) NGẦM giả định thứ tự đến gần đúng thứ tự thời gian thật, nhưng kiến trúc Kafka hiện tại không đảm bảo điều đó cho bất kỳ topic đa-partition nào.

## 3. Missing Capability

**#R2-4 — Không có Distributed Coordination / Scheduling**
Discovery mỗi 1h, `sigma_calibrator` mỗi ngày, confidence decay mỗi ngày — đều là vòng lặp `asyncio` nội bộ trong từng process, không có leader-election, không có tenant-sharding. WS6 (tách Execution/Knowledge Plane) và mục tiêu productization ngầm định sẽ cần multi-replica ở tương lai gần — nhưng **không WS nào trong 13 task hiện có đề cập điều gì xảy ra khi `omni-fullstack` chạy >1 replica**: mỗi replica sẽ tự chạy `sigma_calibrator` cho CÙNG tập tenant, gây tính toán trùng lặp, và tệ hơn nếu 2 replica cùng lúc chạy `decay_confidence()` cho cùng 1 host — race condition ghi đè `add_confidence`.

**#R2-5 — Không có Policy Compiler / policy-as-data**
Toàn bộ policy (risk-class matrix trong `tier_gate.py`, namespace allowlist trong `mutate_governance.py`, ngưỡng blast-radius trong `blast_radius.py`) là hằng số/if-else Python hardcode. Không có biểu diễn khai báo (YAML/Rego/DB-versioned) nào để: (a) test policy độc lập với code, (b) rollback 1 thay đổi policy mà không rollback cả deploy, (c) tuỳ biến policy theo từng khách hàng (yêu cầu tối thiểu cho bất kỳ sản phẩm "enterprise" nào). Mọi thay đổi policy = code change + full redeploy + full test suite — không có "policy versioning" nào tồn tại dù đây là 1 trong các câu hỏi bắt buộc của mục 10.

**#R2-6 — Không có Simulation / What-if**
Không có cơ chế nào để chạy thử 1 thay đổi policy hoặc 1 mutation đề xuất lên World Model/blast-radius mà KHÔNG thực thi thật, để validate trước khi go-live. Với 1 hệ thống có quyền tự động mutate hạ tầng khách hàng, việc thay đổi ngưỡng blast-radius hay tier matrix mà không có cách nào "chạy thử trên lịch sử incident đã có" trước khi deploy là 1 khoảng trống an toàn thật.

## 4. Scalability — cái gì gãy trước ở 10/100/1000 tenant, 5000 agent

**#R2-7 (CRITICAL) — LLM inference (Ollama) là bottleneck đầu tiên, không phải Postgres như framing ngầm định của Round 1**
Theo lịch sử phiên đã ghi nhận (Đ7, memory): Ollama chạy `-np 1` (1 slot suy luận đồng thời), trên 1 máy MacBook (public plane docs xác nhận "core vẫn chạy trên MacBook"). Toàn bộ Reasoning pipeline (mọi tenant, mọi domain) chia sẻ **1 slot suy luận duy nhất**. Ở quy mô 10 tenant thật (không phải 3 VM lab) đã đủ để hàng đợi LLM bão hoà liên tục — đã QUAN SÁT THẬT hiện tượng này ở Đ6 (46% lượt lỗi do bão đồng thời, chỉ với vài chục phiên chồng lấn trong 14 phút, KHÔNG PHẢI 10 tenant thật). Không WS nào trong 13 task đề cập năng lực LLM — toàn bộ narrative "scale lên 1000 tenant" trong 3 tài liệu trước ngầm giả định compute layer co giãn được, nhưng nó là 1 điểm nghẽn cứng duy nhất.

**#R2-8 (CRITICAL) — Head-of-line blocking: 1 tenant chậm chặn toàn bộ pipeline mutate của MỌI tenant khác**
`blast_radius.py` gọi K8s API **đồng bộ** bên trong vòng xử lý của **1 consumer duy nhất** trên topic phần lớn 1-partition (`omni-actions`). Nếu K8s API server của 1 tenant (hoặc chính cluster Omni) chậm/treo — đúng lúc dễ xảy ra nhất là **trong 1 incident thật, khi Omni cần quyết định mutate** — toàn bộ hàng đợi mutate của MỌI tenant khác bị chặn phía sau, vì chỉ có 1 consumer xử lý tuần tự. Đây là noisy-neighbor nghiêm trọng nhất trong toàn kiến trúc, trực tiếp mâu thuẫn với mục tiêu productization multi-tenant.

**#R2-9 — Redis 2GB + `allkeys-lru` là trần cứng cho MỌI capability cùng lúc (RAG HNSW + audit chain + KPI + session + registry...)**
Không phải Postgres (dữ liệu quan hệ scale tốt tới hàng triệu dòng) mà chính **Redis** mới là nơi co giãn kém nhất: RAG HNSW index tăng tuyến tính theo corpus mỗi tenant, tăng thêm KPI ZSET/session/registry mỗi tenant/agent — tất cả trong 1 instance 2GB duy nhất. Ở quy mô 100+ tenant có corpus RAG thật, 2GB sẽ chạm trần rất sớm — và khi chạm trần, `allkeys-lru` **âm thầm evict bất kỳ key nào**, kể cả `audit_chain:*` (xem mục 6).

## 5. Failure Analysis

**#R2-10 (CRITICAL) — Redis chết = tier resolution crash (không fail-closed graceful) — mục 2 (#R2-2)**

**#R2-11 — Kafka chết: outbox agent gây retry-storm khi phục hồi**
`remote_agent/outbox.py` spool ra đĩa khi gateway unreachable, flush theo thứ tự khi có lại — nhưng không thấy cơ chế backpressure/rate-limit phía Gateway cho lượng burst evidence dồn về cùng lúc khi NHIỀU agent đồng loạt reconnect sau downtime Kafka/gateway diện rộng. Không phải deadlock, nhưng là retry-storm tiềm ẩn chưa có admission control.

**#R2-12 — Postgres chết SAU KHI đã kết nối (không phải lúc khởi động)**
Fix Đ12 (`_connect_admin_pool_with_retry`) chỉ xử lý race lúc STARTUP. Không có bằng chứng nào cho thấy mọi call site dùng `admin_repo` xử lý graceful khi Postgres chết GIỮA CHỪNG (sau khi pool đã khoẻ) — retry ở Đ12 không chạy lại vì pool coi như đã "ready". Đây là gap availability khác hẳn gap data-loss (backup) mà Round 1 đã nêu.

**#R2-13 — Change Ledger lag (WS8) trả lời "không có gì đổi" một cách TỰ TIN SAI**
Thiết kế `get_changes_before()` (Round 1, đã giáng cấp) không có trường staleness/lag. Nếu ETL nạp từ discovery-diff/CRAT bị trễ, Reasoning nhận câu trả lời "không có thay đổi gần đây" đầy tự tin trong khi thực tế CÓ — false negative đúng ngay trong capability được thiết kế để chống blind-spot.

## 6. Data Ownership

**#R2-14 (CRITICAL) — `audit_chain:*` (CRAT) sống chung Redis instance với chính sách `allkeys-lru`**
Đây không phải vấn đề "nhiều owner" (CRAT vẫn đúng 1 owner: `chain_writer.py`) mà là **owner đúng nhưng storage policy sai**: dữ liệu tamper-evident, retention vô thời hạn theo thiết kế (SOX/PCI) lại nằm trong 1 Redis instance được cấu hình **evict bất kỳ key nào khi đầy bộ nhớ**, không phân biệt `audit_chain:*` với cache tạm thời khác. Nếu Redis chạm 2GB, block CRAT có thể bị evict âm thầm — phá vỡ tính liên tục của hash-chain (1 block mất giữa chuỗi làm mọi block SAU đó không verify được nữa dù bản thân chúng còn nguyên) — hậu quả compliance nghiêm trọng nhất trong toàn bộ review.

**#R2-15 — "Learning" là ≥2 cơ chế không liên quan, bị gộp chung 1 tên trong kiến trúc**
`autonomous_feedback_loop._upsert_action_experience_on_success` (học kinh nghiệm hành động, trigger theo execute) và `remote_host_baseline.add_confidence` (học ngưỡng baseline, trigger theo đếm mẫu) là 2 pipeline hoàn toàn độc lập — khác store, khác trigger, khác cadence — nhưng cả `OMNI_V2_ARCHITECTURE_REDESIGN.md` (Phase 2) lẫn `OMNI_V2_IMPLEMENTATION_PLAN.md` (WS3) đều nói về "Learning" như 1 capability duy nhất. Nếu ai đó thiết kế "1 module Learning thống nhất" theo đúng tài liệu, sẽ phát hiện giữa chừng rằng không có gì để thống nhất — 2 khái niệm khác nhau đang dùng chung nhãn.

## 7. Event Architecture

Xem #R2-3 (0 partition key trên MỌI producer call site — không ordering, không correlation-id-để-partition cho bất kỳ event nào). Bổ sung:

**#R2-16 — Không event nào trong hệ thống hiện tại có `schema_version` (trừ agent envelope, đã bị Round 1 hoãn)**
`omni-actions`, `omni-action-feedback`, `omni-diagnostic-evidence`, CRAT payload — không có field version nào. Đây chính là lý do cuộc di trú lane→domain (2026-07-30) phải cần hẳn 1 runbook + migration Postgres riêng thay vì chỉ là 1 thay đổi field thông thường — vấn đề mang tính hệ thống sẽ lặp lại ở mọi lần đổi schema tương lai, không phải sự cố 1 lần.

## 8. Bounded Context Integrity

**#R2-17 — `evidence_consumer.py` (3578 dòng) là kẻ vi phạm ranh giới context THẬT SỰ, không phải `omni_worker.py`**
Round 1 tập trung vào `omni_worker.py` (god object điều phối). Nhưng `evidence_consumer.py` mới là nơi **trực tiếp** chạm vào ≥4 bounded context khác nhau trong cùng 1 file: Knowledge (RAG recall), Reasoning (gọi LLM), System Twin (`system_twin_context.build_system_twin_block`), và ngầm cả Governance (quyết định có promote/dispatch hay không). WS5 chỉ nhắm `omni_worker.py` — không WS nào trong 13 task đụng tới file này, dù nó là ứng viên god-object có bằng chứng cụ thể hơn (3578 dòng so với 1420 dòng của `omni_worker.py`).

**#R2-18 — World Model (Twin) sau WS7 sẽ bị đọc bởi 2 consumer có yêu cầu độ tin cậy khác hẳn nhau, tạo correlated failure mode**
`system_twin_context.py` (Reasoning, thông tin bổ trợ, fail-open) và `blast_radius.py` (sau WS7, an toàn, phải fail-closed) đều đọc CÙNG 1 nguồn `SystemModel`. Nếu Twin sai/cũ đúng lúc 1 tenant vừa onboard hoặc mới có thay đổi hạ tầng chưa kịp discover — **cả 2 hệ quả xảy ra cùng lúc**: LLM suy luận sai NGỮ CẢNH và (nếu điều kiện confidence/freshness của WS7 vô tình pass) an toàn bị đánh giá sai — cùng 1 nguyên nhân gốc gây lỗi kép đúng vào thời điểm rủi ro cao nhất (ngay sau 1 thay đổi hạ tầng thật).

## 9. Workstream Order

**#R2-19 — Thiếu hẳn 1 WS cho Redis HA/backup** (song song với việc Round 1 đã có WS must-fix cho Postgres) — Redis đang gánh nhiều capability critical-path hơn Postgres (mục 4, 6) nhưng không có task nào theo dõi rủi ro SPOF của nó.

**#R2-20 — Thiếu hẳn 1 WS cho LLM capacity** — không nằm trong backlog dưới bất kỳ hình thức nào, dù đây là bottleneck đầu tiên theo phân tích mục 4.

**#R2-21 — WS8 (Change Ledger) nên có yêu cầu bắt buộc "staleness indicator" trước khi tích hợp vào Reasoning prompt** — hiện task #9 (đã sửa theo Round 1) không đề cập gap này.

**#R2-22 — Việc thêm Kafka partition-key nên là 1 WS riêng, làm CÙNG SÓNG với WS1 (sửa import ngược)** — đây là hygiene nền tảng tương tự tinh thần WS0/WS1, và nên xong TRƯỚC WS2 (thêm event `DECISION_RENDERED`) và WS8 (Change Ledger phụ thuộc đúng thứ tự thời gian) để không tạo thêm event thiếu key ngay từ đầu.

## 10. Nếu là sản phẩm thương mại

**#R2-23 — Không tenant resource isolation nào ngoài tiền tố key Redis** — 1 tenant tải cao (nhiều RAG corpus, nhiều incident đồng thời) có thể chiếm hết 2GB Redis dùng chung hoặc chiếm trọn slot LLM `-np 1` dùng chung, làm suy giảm chất lượng dịch vụ cho MỌI tenant khác — vi phạm trực tiếp kỳ vọng cơ bản nhất của multi-tenant SaaS.

**#R2-24 — Có bảng `tenant_plan_entitlements` (migration 0009) gợi ý đã có khái niệm "plan"/gói dịch vụ, nhưng không có workstream nào nối nó với enforcement thực tế** (rate-limit LLM theo tier gói, quota RAG corpus theo gói...) — hạ tầng billing/quota đã bắt đầu ở tầng dữ liệu nhưng bỏ dở, không route vào bất kỳ nơi nào enforce giới hạn thật.

**#R2-25 — Không multi-region, không zero-downtime upgrade path nào được đề cập** — chấp nhận được ở giai đoạn lab, nhưng hoàn toàn vắng mặt khỏi backlog 13 task dù toàn bộ 3 tài liệu trước định hướng rõ ràng vào "sản phẩm" (portal provider/tenant đã build, public plane đã có domain riêng) — khoảng cách giữa tham vọng sản phẩm và backlog kỹ thuật là có thật.

---

# Final Verdict

## Danh sách đầy đủ theo mức độ (25 issue, vượt yêu cầu tối thiểu 10)

### CRITICAL
- #R2-2 / #R2-10 — `resolve_tier()` không fail-closed graceful khi Redis mất kết nối
- #R2-7 — LLM (Ollama `-np 1`, 1 máy) là bottleneck đầu tiên khi scale, không nằm trong backlog
- #R2-8 — Head-of-line blocking: 1 tenant chậm chặn mutate pipeline của mọi tenant khác
- #R2-9 — Redis 2GB là trần cứng chung cho RAG+audit+KPI+session+registry
- #R2-14 — `audit_chain:*` sống trong Redis `allkeys-lru` — có thể bị evict âm thầm, phá hash-chain

### HIGH
- #R2-1 — Decision 3-owner không đồng bộ, CRAT ghi trước khi Kafka publish được xác nhận
- #R2-3 / #R2-16 — 0 partition-key trên mọi Kafka producer, 0 schema-version ngoài agent envelope
- #R2-4 — Không có distributed coordination/scheduling — multi-replica sẽ trùng lặp job
- #R2-13 — Change Ledger lag trả lời sai tự tin, không staleness indicator
- #R2-17 — `evidence_consumer.py` (3578 dòng) là god-object/context-violator thật, không nằm trong WS nào
- #R2-19, #R2-20, #R2-22 — thiếu WS cho Redis HA, LLM capacity, Kafka partition-key
- #R2-23 — không tenant resource isolation

### MEDIUM
- #R2-5 — thiếu Policy Compiler/policy-as-data
- #R2-6 — thiếu Simulation/what-if
- #R2-11 — retry-storm tiềm ẩn khi outbox agent flush hàng loạt lúc phục hồi
- #R2-12 — Postgres chết giữa chừng (không phải lúc start) chưa có story graceful degradation
- #R2-15 — "Learning" là 2 cơ chế không liên quan bị gộp 1 tên
- #R2-18 — World Model 1 nguồn, 2 consumer yêu cầu độ tin cậy khác nhau → correlated failure
- #R2-21 — WS8 thiếu yêu cầu staleness indicator tường minh
- #R2-24 — `tenant_plan_entitlements` tồn tại nhưng không enforce
- #R2-25 — thiếu multi-region/zero-downtime upgrade trong toàn bộ backlog

---

## Top 10 theo ROI (severity cao × effort sửa thấp, ưu tiên trước)

| # | Issue | Effort | Vì sao ROI cao |
|---|---|---|---|
| 1 | #R2-2/10 — thêm `try/except` quanh Redis read trong `resolve_tier()` | **Rất thấp** (mirror đúng pattern `_apply_plan_ceiling` đã có sẵn 20 dòng dưới cùng file) | CRITICAL → fix gần như miễn phí, đóng đúng lỗ hổng nghiêm trọng nhất về availability |
| 2 | #R2-14 — tách `audit_chain:*` khỏi policy `allkeys-lru` (đổi maxmemory-policy scoped, hoặc logical DB riêng, hoặc `noeviction` cho prefix đó) | Thấp-Trung bình | CRITICAL compliance risk, không cần đổi kiến trúc, chỉ đổi cấu hình Redis + có thể cần tách key namespace |
| 3 | #R2-22 — thêm partition key (`tenant_id`/`resource_id`) vào mọi `send_and_wait` | Trung bình (cơ học, nhiều call site nhưng pattern đơn giản) | Đóng gap nền tảng cho toàn bộ Event Architecture, nên làm TRƯỚC WS2/WS8 để không tạo thêm nợ |
| 4 | #R2-13/21 — thêm `last_synced_at`/staleness field vào response `get_changes_before()` | Thấp | Rẻ, đóng đúng lúc trước khi WS8 tích hợp vào Reasoning prompt |
| 5 | #R2-8 — thêm timeout + circuit-breaker quanh lời gọi K8s API trong `blast_radius.py` | Trung bình | Bảo vệ fairness đa-tenant ngay lập tức, không cần đổi topology |
| 6 | #R2-17 — thêm WS mới (hoặc mở rộng WS5) cho `evidence_consumer.py` | Trung bình-Cao (3578 dòng) nhưng CHỈ CẦN GHI NHẬN vào backlog trước, chưa cần làm ngay | God-object thật lớn hơn cái đang được sửa — phải vào backlog để không bị quên |
| 7 | #R2-1 — đổi thứ tự Producer: gửi Kafka trước, ghi CRAT sau khi có ack (hoặc thêm reconciliation sweep định kỳ) | Trung bình | Đóng gap consistency giữa audit log và hành động thật |
| 8 | #R2-19 — ghi nhận Redis HA vào backlog như 1 WS chính thức (chưa cần làm ngay, nhưng phải được theo dõi) | Thấp (chỉ cần tạo task) | SPOF nghiêm trọng nhất hệ thống hiện chưa hề được theo dõi ở đâu |
| 9 | #R2-20 — ghi nhận LLM capacity vào backlog như 1 WS chính thức | Thấp (chỉ cần tạo task + 1 tài liệu đo capacity thật) | Ngăn "1000 tenant" trở thành lời hứa suông trong tài liệu kiến trúc |
| 10 | #R2-4 — đặc tả rõ (không cần code ngay) chiến lược leader-election/tenant-sharding cho multi-replica TRƯỚC KHI WS6 triển khai thật | Thấp (đặc tả) | Ngăn WS6 (đã duyệt ở Round 1) tự tạo ra bug trùng lặp job ngay khi có replica thứ 2 |

**Không dừng ở 10** — 15 issue còn lại (#R2-5, #R2-6, #R2-9, #R2-11, #R2-12, #R2-15, #R2-18, #R2-23, #R2-24, #R2-25...) vẫn là nợ kiến trúc thật, ưu tiên thấp hơn về ROI ngắn hạn nhưng KHÔNG được coi là đã đóng — cần quay lại trước khi tuyên bố "production-ready cho multi-tenant thương mại".
