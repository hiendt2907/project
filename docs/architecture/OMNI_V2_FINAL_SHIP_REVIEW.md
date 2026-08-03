# Omni v2 — FINAL SHIP REVIEW (Investment Committee)

**Hội đồng:** CTO + Principal Distributed Systems Architect + Staff Production SRE + Enterprise Platform Architect
**Câu hỏi duy nhất:** Nếu đây là startup của chính tôi, tôi có ký duyệt để đội bắt đầu roadmap này không?
**Input:** `OMNI_V2_ARCHITECTURE_REDESIGN.md`, `OMNI_V2_IMPLEMENTATION_PLAN.md`, `OMNI_V2_RED_TEAM_REVIEW.md`, `OMNI_V2_RED_TEAM_ROUND2_REVIEW.md` — được phép phủ định bất kỳ kết luận nào ở 4 file trên nếu bằng chứng mới nói khác.
**Bằng chứng mới trong lượt này** (live cluster, không chỉ đọc yaml): `redis-0` — **1 pod duy nhất**, 35 restart/55 ngày, `used_memory=875.75M / maxmemory=2.00G` (44% đã dùng ở quy mô lab gần như không tải thật) — xác nhận sống, không phải suy đoán. `kafka-685dc55dfb-68sz9` — **1 pod duy nhất**, 53 restart/81 ngày. `K8sBlastReader.__init__` tạo `CoreV1Api()`/`AppsV1Api()` **không có timeout nào được set tường minh**. `siem_correlation/chain.py`: `sorted(members, key=lambda m: m.ts)` — sắp xếp theo **field `ts` trong payload, không phải thứ tự Kafka đến** — 1 finding Round 2 cần hạ mức (xem Phase 1).

---

# PHASE 1 — Loại bỏ false positive (Round 1 + Round 2)

| Cụm finding | Verdict | Lý do |
|---|---|---|
| God object `omni_worker.py` (1420 dòng), circular dependency `pkg/anomaly/rag→workers` | **VALID** | Đã gây bug production thật (crash-loop `omni-onboarding`, Đ12), đã verify `grep` xác nhận import ngược tồn tại thật |
| `AutonomyControlPlane`/`GovernanceEngine`/`AutonomyEngine` nên xây mới | **INVALID** (đã tự phủ định ở Round 1) | `mutate_governance.py`+`tier_gate.py` đã tách đúng, có bằng chứng lịch sử (drift 2026-07-02) rằng phân tán check là tính năng, không phải bug. Giữ nguyên kết luận Round 1: không xây |
| "Change Intelligence bounded context" | **VALID nhưng bị giáng cấp đúng** (Round 1 tự sửa) | Insight đúng (thiếu thật), tên gọi ban đầu sai tầm — Round 1 đã hạ xuống 1 bảng + 1 module, giữ nguyên |
| System Twin (`aoip`) là "demo code" | **INVALID** (Round 1 tự phủ định, đã verify lại: 58 commit, 95 test file, đã cắm dây `evidence_consumer.py`) | Xác nhận lại đúng — không lặp lại lỗi này |
| Landmine `teardown-omni-postgres` | **VALID, đã đọc trực tiếp script + yaml** | Script scale `omni-worker`/`omni-watchdog` (tên deployment đã retired) rồi xoá đúng CNPG cluster đang backing `omni_admin` — rủi ro thật, chưa có guard |
| `resolve_tier()` không fail-closed khi Redis mất kết nối | **VALID, đã đọc code trực tiếp** | Xác nhận lại: không có `try/except` quanh `read_tier_cached`, trong khi `_apply_plan_ceiling` 20 dòng dưới có — bất nhất thật trong 1 file |
| `audit_chain:*` sống trong Redis `allkeys-lru` 2GB | **VALID, NÂNG MỨC bằng bằng chứng sống** | Lượt này verify TRỰC TIẾP trên cluster: `used_memory=875.75M/2.00G` (44%) ở quy mô gần-zero tải thật — không còn là rủi ro lý thuyết, là xu hướng đã quan sát được |
| 0 partition-key trên mọi Kafka producer → phá SIEM correlation sequence-score | **PARTIALLY VALID, HẠ MỨC** | Xác nhận đúng 0 call site có `key=`. NHƯNG đọc `chain.py` cho thấy correlation sort theo **field `ts` trong payload**, không theo thứ tự Kafka đến — SIEM correlation **đã có khả năng chống chịu** với mất thứ tự partition ở phần lớn trường hợp (chỉ rủi ro thật khi 2 sự kiện trùng đúng 1 giây). Gap vẫn có thật (không key = không group đúng theo tenant cho mục đích khác, ví dụ throughput/backpressure), nhưng lý do "phá causality" ở Round 2 là **phóng đại** |
| Head-of-line blocking — `blast_radius.py` gọi K8s API đồng bộ, không timeout | **VALID, KHÔNG hạ mức** | Verify code trực tiếp: `K8sBlastReader.__init__` không set timeout nào — xác nhận rủi ro treo vô thời hạn là thật, không phải suy đoán |
| LLM/Ollama 1 instance là bottleneck đầu tiên | **VALID** | Đã có bằng chứng thật từ lịch sử phiên (Đ6: 46% lỗi do bão đồng thời chỉ với vài chục phiên) — không cần verify lại |
| `evidence_consumer.py` (3578 dòng) là god-object thật, không nằm trong WS nào | **VALID nhưng SEVERITY QUÁ CAO ở Round 2** | Kích thước file lớn không tự nó là outage risk — khác hẳn `omni_worker.py` (có bằng chứng crash-loop cụ thể). Đây là nợ kỹ thuật thật, không phải BLOCKER |
| Decision có 3 owner không đồng bộ (CRAT/trace-stage/Kafka) | **PARTIALLY VALID, HẠ MỨC** | Thứ tự ghi (CRAT trước Kafka) là thật, nhưng "MUTATION_ENQUEUED" ghi trước khi Kafka publish thành công **không sai về mặt ngữ nghĩa** (nó ghi đúng "quyết định enqueue đã xảy ra") — thiếu 1 sự kiện terminal-state nối tiếp (`MUTATION_DISPATCH_FAILED`) khi Kafka publish fail, không phải "audit log nói dối" như khung Round 2 ngụ ý |
| Leader-election/tenant-sharding phải xong TRƯỚC WS6 (`blockedBy`) | **INVALID — tự phủ định** | WS6 chỉ tách Deployment (Execution ↔ Knowledge Plane), **không đòi hỏi multi-replica ngay**. Cả 2 Deployment mới vẫn có thể chạy `replicas: 1`. Leader-election chỉ cần thiết KHI quyết định scale >1 replica — đó là điều kiện của MỘT BƯỚC SAU WS6, không phải của chính WS6. Gỡ `blockedBy` này |
| Policy Compiler / Simulation / multi-region / billing / quota / license / plugin ecosystem | **VALID như quan sát, INVALID như điều kiện duyệt roadmap này** | Đều là khoảng trống thật cho tham vọng "Autonomous SRE OS"/"Commercial SaaS" — nhưng roadmap 21 task hiện tại CHƯA BAO GIỜ tuyên bố sẽ giao các capability này. Không thể coi là "thiếu sót của roadmap" khi roadmap không hứa hẹn nó — đây là backlog SẢN PHẨM giai đoạn sau, không phải điều kiện duyệt giai đoạn này |
| Redis HA / Kafka HA thiếu | **VALID, giữ nguyên mức nghiêm trọng** | Verify sống: 1 pod duy nhất mỗi loại, đã restart 35 và 53 lần trong lịch sử — không phải rủi ro lý thuyết, là SPOF đã và đang gây gián đoạn định kỳ (mỗi lần restart = mini-outage cho mọi capability phụ thuộc) |
| "Learning" là 2 cơ chế không liên quan gộp 1 tên | **VALID nhưng MEDIUM, không phải kiến trúc lỗi** | Đây là vấn đề đặt tên/tài liệu (nhầm lẫn khi đọc), không phải bug runtime — hạ xuống đúng mức "làm rõ tài liệu", không cần thiết kế lại |
| **Meta-finding mới, chưa ai nêu**: 4 tài liệu kiến trúc (~4000+ dòng tổng) cho 1 roadmap ước lượng thực chất ~15-20 hạng mục kỹ thuật | **VALID, nêu ở Phase 5** | Tỷ lệ ceremony/deliverable đang lệch — xem Phase 5 |

---

# PHASE 2 — Chỉ giữ issue thực sự chặn production platform

Sau khi lọc theo đúng 6 tiêu chí (outage, data corruption, security, scalability, operability, commercial SaaS) — bỏ toàn bộ style/preference/tài liệu-thuần:

1. Landmine `teardown-omni-postgres` (data corruption/outage risk thật, xác suất thấp nhưng hậu quả nghiêm trọng — xoá nhầm DB production lab)
2. `resolve_tier()` không fail-closed khi Redis mất kết nối (outage — Redis chết kéo theo tier resolution crash thay vì degrade)
3. `audit_chain:*` trong Redis `allkeys-lru` (data corruption/compliance — đã có bằng chứng sống 44% dung lượng)
4. God object `omni_worker.py` + circular dependency (đã gây outage thật 1 lần — crash-loop)
5. Head-of-line blocking `blast_radius.py` không timeout (outage/scalability — 1 tenant chặn mọi tenant)
6. Redis/Kafka single-pod, không HA (outage — SPOF đã restart 35/53 lần)
7. LLM/Ollama 1 instance (scalability/commercial-SaaS — chặn đường lên multi-tenant thật)
8. Backup/restore thiếu cho `omni-postgres` (data corruption — không phục hồi được nếu mất dữ liệu)
9. Không tenant resource isolation (commercial-SaaS/security — 1 tenant chiếm hết tài nguyên chung)

**Loại khỏi danh sách chặn** (không thoả tiêu chí, chuyển xuống Phase 3 mức thấp hơn hoặc bỏ hẳn): `evidence_consumer.py` kích thước file, "Learning" đặt tên nhầm, Decision 3-owner (đã hạ mức ngữ nghĩa), Kafka partition-key (đã hạ mức do SIEM correlation tự chống chịu), Policy Compiler/Simulation/multi-region/billing (ngoài phạm vi roadmap hiện tại).

---

# PHASE 3 — Phân loại lại

## BLOCKER (không được merge roadmap nếu chưa sửa)

- **Landmine `teardown-omni-postgres`** — phải trung hoà trước khi bất kỳ WS nào (WS3/WS8, và thực ra cả toàn bộ hoạt động vận hành thường ngày) tiếp tục thêm dữ liệu vào cùng Postgres.
- **`resolve_tier()` fail-closed fix** — effort gần như 0 (mirror pattern có sẵn cùng file), không có lý do hợp lệ để trì hoãn quá vài giờ làm việc.

## MUST FIX BEFORE GA (code được, nhưng phải xong trước khi tuyên bố production-ready)

- Tách `audit_chain:*` khỏi policy `allkeys-lru` (đổi maxmemory-policy scoped hoặc tách logical DB/instance).
- WS5 (god-object `omni_worker.py` → Capability Registry).
- WS1 (sửa 5 import ngược) + WS0 (import-linter, làm sau WS1).
- Timeout + circuit-breaker cho `blast_radius.py` K8s call.
- Backup/restore cho `omni-postgres`.
- WS2 (Decision Transparency Layer — bản đã thu nhỏ Round 1), KHÔNG cần thêm yêu cầu "sửa thứ tự ghi CRAT/Kafka" làm điều kiện chặn (đã hạ mức ở Phase 1) — có thể làm như 1 cải tiến nhỏ đi kèm, không phải blocker.

## CAN FIX AFTER GA (chấp nhận technical debt có kiểm soát)

- Redis/Kafka HA (SPOF thật nhưng hệ thống ĐANG chạy được ở quy mô lab/pilot với SPOF này — chấp nhận được cho Enterprise Pilot, không chấp nhận được nếu tiến lên Commercial SaaS — xem Phase 6).
- LLM/Ollama capacity scaling (tương tự — chặn Commercial SaaS, không chặn Enterprise Pilot single-tenant).
- WS6 (Execution/Knowledge Plane split) — giá trị thật (giảm blast-radius bảo mật) nhưng không phải outage risk nếu trì hoãn.
- WS7 (System Twin → blast-radius, polarity union) — cải tiến chất lượng quyết định, không phải fix bug.
- WS9 (Remote Agent SDK chuẩn hoá OBSERVED-only) — đúng nguyên tắc sản phẩm nhưng không phải outage risk hiện tại.
- Kafka partition-key — làm khi có thời gian, không khẩn cấp sau khi đã xác nhận SIEM correlation tự chống chịu phần lớn.
- `evidence_consumer.py` tách theo bounded context.

## NICE TO HAVE (không ảnh hưởng production)

- WS3 (Operational Memory — lazy replay, archive-chỉ-trace-promote).
- WS4 (Agent Platform — Ed25519 signing, version-compat gate) — **lưu ý: đây được xếp NICE TO HAVE ở góc độ "chặn production", nhưng vẫn là ưu tiên bảo mật thật nên nên làm sớm** dù không chặn GA theo đúng nghĩa outage/data-corruption.
- WS8 (Change context) — giá trị sản phẩm thật nhưng không phải production risk nếu thiếu.
- WS10 (dọn manifest chết).
- Policy Compiler, Simulation, multi-region, billing/quota, plugin ecosystem — roadmap giai đoạn sau, không phải backlog của lần duyệt này.

---

# PHASE 4 — Workstream-by-workstream

| WS | Đánh giá | Ghi chú |
|---|---|---|
| WS0 | **KEEP**, thứ tự sau WS1 | Không đổi so với Round 1 |
| WS1 | **KEEP**, ưu tiên cao (MUST FIX BEFORE GA) | Rẻ, rõ ràng, không tranh cãi |
| WS2 | **KEEP nhưng GIẢM YÊU CẦU** | Bỏ điều kiện "phải sửa thứ tự ghi CRAT/Kafka" làm blocker (Phase 1 đã hạ mức) — giữ lại như cải tiến tự chọn đi kèm |
| WS3 | **KEEP, hạ xuống NICE TO HAVE** | Giá trị thật nhưng không cấp bách |
| WS4 | **KEEP MỘT PHẦN (đã thu nhỏ Round 1), tách phần Ed25519 ra làm riêng, ưu tiên cao hơn phần còn lại** | Bảo mật thật (vá supply-chain risk) xứng đáng làm sớm dù không phải "blocker" theo định nghĩa chặt của Phase 3 |
| WS5 | **KEEP, MUST FIX BEFORE GA** | Duy nhất trong nhóm có bằng chứng outage thật đã xảy ra |
| WS6 | **KEEP, CAN FIX AFTER GA, GỠ `blockedBy` #20** | Không cần leader-election để tách Deployment (Phase 1 đã phủ định điều kiện chặn cũ) |
| WS7 | **KEEP, CAN FIX AFTER GA** | Đúng polarity (union) từ Round 1, không có gì thêm cần sửa ngoài timeout ở blast_radius (đã tách thành yêu cầu riêng, không phải điều kiện của WS7) |
| WS8 | **KEEP, hạ xuống NICE TO HAVE** | Đúng quy mô 1 bảng + 1 module như Round 1 đã giáng cấp |
| WS9 | **KEEP, CAN FIX AFTER GA** | Rủi ro vận hành cao nhất (VM khách hàng), không cần vội |
| WS10 | **MERGE vào công việc dọn dẹp chung, KHÔNG cần track như 1 WS riêng** | Việc nhỏ, không xứng đáng có task/WS riêng biệt — xử lý như 1 PR nhỏ bất kỳ lúc nào rảnh |

**Không thêm WS mới.** Round 2 đề xuất backlog cho Redis HA/LLM capacity/leader-election-spec — những mục này chuyển thành **theo dõi rủi ro đã biết** (documented known risk), không cần thành WS chính thức trong roadmap lần này vì chúng thuộc CAN-FIX-AFTER-GA, không chặn việc bắt đầu.

---

# PHASE 5 — Nói thẳng: roadmap còn overengineering ở đâu

- **"Bounded context" cho Change Intelligence đã đúng bị hạ, nhưng ngay cả việc gọi nó là 1 "capability" riêng trong Phase 2 của redesign doc vẫn hơi nặng nề** — đây thực chất là 1 SQL join thêm cho `services/analyst/`.
- **Operational Memory (`/trace/{id}/replay`) chưa cần trong roadmap này** — chưa có bằng chứng operator nào thực sự cần "replay" ngoài việc đọc log/CRAT thủ công hiện tại. Xây trước khi có nhu cầu xác nhận là đúng định nghĩa "giải quyết vấn đề tưởng tượng."
- **Agent Registry Postgres hoá, Lifecycle states, Canary automation (đã hoãn ở Round 1) — đúng, tiếp tục hoãn, không có lý do gì để hồi sinh ở lần duyệt này.**
- **4 tài liệu kiến trúc (~4000+ dòng) cho roadmap thực chất ~10-12 hạng mục kỹ thuật cụ thể sau khi lọc (Phase 2-4)** — đây là quan sát quan trọng nhất của Phase 5: **quy trình review đã tiêu tốn nhiều công sức hơn bản thân phần việc cần làm.** Không sai khi làm kỹ ở vòng đầu (god-object và circular-dependency là phát hiện thật, đáng giá), nhưng tới vòng thứ 4 (tài liệu này), tỷ suất phát hiện-mới/trang-viết đã giảm mạnh — phần lớn Phase 1 ở trên là XÁC NHẬN LẠI hoặc HẠ MỨC finding cũ, không phải phát hiện mới. **Khuyến nghị: dừng vòng review kiến trúc tại đây, chuyển sang thực thi.** Nếu cần review tiếp, nên review CODE THẬT sau khi implement (code review), không phải review thêm tài liệu kiến trúc.
- **21 task tracked cho 1 roadmap mà sau Phase 2-4 chỉ còn ~10-12 hạng mục thật sự cần** — số lượng task nên được gộp lại (xem Phase 4: WS10 merge, các backlog-only item của Round 2 chuyển thành ghi chú rủi ro thay vì task riêng) trước khi bắt đầu, để không tạo ảo giác "còn nhiều việc" khi thực chất là ~10 việc cụ thể.

---

# PHASE 6 — Đánh giá Product

**Nếu hoàn thành đúng roadmap đã lọc (Phase 3-4): Omni đạt mức Enterprise Pilot, KHÔNG PHẢI Commercial SaaS.**

Lý do dựa trên bằng chứng đã verify:
- **Internal Tool**: đã VƯỢT mức này — có test suite thật (7348 test), có audit trail thật (CRAT hash-chain + Ed25519), có multi-tenant data model ở tầng Postgres/Redis-key-prefix, có portal provider/tenant thật đang chạy.
- **Enterprise Pilot**: đạt được SAU KHI xong BLOCKER + MUST-FIX-BEFORE-GA (Phase 3) — 1 khách hàng triển khai riêng (dedicated Redis/Kafka/Ollama instance cho họ) hoàn toàn khả thi và an toàn ở mức này, với SPOF đã biết và có thể chấp nhận được (đội vận hành hiểu rõ rủi ro, có runbook).
- **Commercial SaaS (multi-tenant dùng chung hạ tầng)**: **CHƯA đạt, và roadmap hiện tại (kể cả bản đầy đủ, không lọc) KHÔNG đưa hệ thống tới đây** — thiếu: tenant resource isolation (đã verify Redis dùng chung 2GB, LLM dùng chung 1 slot), billing/quota enforcement (bảng `tenant_plan_entitlements` tồn tại nhưng không nối vào enforcement nào), Redis/Kafka HA thật (không chỉ backup, mà là failover). Đây không phải lỗi của roadmap — roadmap chưa bao giờ nhắm tới mục tiêu này ở giai đoạn này, nhưng CẦN NÓI THẲNG để tránh kỳ vọng sai từ phía business/sales.
- **Mission Critical Platform**: còn xa hơn Commercial SaaS — cần thêm multi-region, zero-downtime upgrade, formal SLA — không nằm trong phạm vi bất kỳ tài liệu nào trong 4 tài liệu đã đọc.

---

# PHASE 7 — Investment Decision

## Verdict

**APPROVE WITH CONDITIONS**

## Blocking Issues

1. Trung hoà landmine `teardown-omni-postgres` (script + Makefile target) — trước khi bất kỳ ai chạm Postgres schema thêm.
2. Fix `resolve_tier()` fail-closed khi Redis mất kết nối — effort ~1 giờ, không có lý do trì hoãn.

Không có blocker thứ 3. Mọi thứ khác (kể cả Redis eviction policy, god-object, HA) đủ nghiêm trọng để MUST-FIX-BEFORE-GA nhưng không đủ để chặn việc BẮT ĐẦU roadmap.

## Roadmap Changes

1. Gỡ `blockedBy: ["20"]` khỏi WS6 — leader-election không phải điều kiện của việc tách Deployment.
2. Gỡ yêu cầu "sửa thứ tự ghi CRAT/Kafka" khỏi điều kiện chặn của WS2 — chuyển thành cải tiến tự chọn.
3. Hạ severity của: `evidence_consumer.py` (MEDIUM, không phải HIGH), Kafka partition-key (LOW-MEDIUM sau khi xác nhận SIEM correlation tự chống chịu, không phải HIGH), Decision 3-owner (MEDIUM, không phải "audit integrity gap" nghiêm trọng).
4. Merge WS10 vào công việc dọn dẹp thường xuyên — không cần track riêng.
5. Chuyển toàn bộ backlog-only item của Round 2 (Redis HA, LLM capacity, leader-election spec, `evidence_consumer.py` refactor) từ "task tracked" sang "rủi ro đã biết, ghi trong 1 tài liệu rủi ro sống" — không cần 8 task riêng cho những gì chưa quyết định làm.
6. **Dừng vòng review kiến trúc tiếp theo** — nếu cần đánh giá thêm, chuyển sang code review sau khi WS1/WS5/2 blocker được implement, không viết thêm tài liệu kiến trúc.

## Estimated Readiness

- **Architecture:** 75% — cấu trúc đúng sau 2 vòng sửa, còn thiếu thiết kế chính thức cho Redis/Kafka HA và LLM capacity (chưa ai thiết kế, chỉ mới "ghi nhận là thiếu").
- **Implementation:** 0% — xác nhận rõ ràng: chưa có 1 dòng code nào của bất kỳ WS nào được viết trong toàn bộ quá trình 5 vòng tài liệu này.
- **Production (v1 hiện tại, đang chạy):** 55% — hệ thống THẬT đang chạy, có test/audit/deploy thật, nhưng có ít nhất 1 landmine sống + nhiều SPOF chưa giải quyết.
- **Commercial SaaS:** 15% — thiếu gần như toàn bộ nền tảng multi-tenancy thật (isolation, quota, HA) dù có khung dữ liệu sơ khởi (`tenant_plan_entitlements`).

## Final Statement

Có, nhưng không phải như đã viết. Tôi sẽ không duyệt 12 tháng engineering cho toàn bộ 4 tài liệu như hiện có — sau khi lọc thật kỹ (Phase 2-4), phần việc CÓ BẰNG CHỨNG THẬT đáng làm chỉ còn khoảng 10-12 hạng mục cụ thể, ước lượng hợp lý 6-8 tuần cho 1-2 kỹ sư, không phải 12 tháng. Phần còn lại của 4 tài liệu (Change Intelligence, Operational Memory, Agent Registry đầy đủ, System Twin polarity, multi-plane split) là backlog sản phẩm hợp lệ nhưng chưa có bằng chứng nào — không phải bug đã xảy ra, không phải incident thật — biện minh cho việc làm ngay bây giờ thay vì chờ nhu cầu khách hàng thật xác nhận.

Tôi ký duyệt phần lõi (2 blocker + 5 must-fix-before-GA: landmine, resolve_tier, Redis eviction policy, god-object refactor, import cycle, blast-radius timeout, Postgres backup) làm ngay. Phần còn lại quay về backlog, ưu tiên lại theo nhu cầu khách hàng thật khi Omni có khách hàng trả tiền đầu tiên ngoài phòng lab — không phải theo thứ tự đã liệt kê trong 4 tài liệu kiến trúc, vốn được viết TRƯỚC KHI có tín hiệu nhu cầu thật nào từ bên ngoài. Đây là lý do tôi ghi "APPROVE WITH CONDITIONS" chứ không phải "APPROVE" trơn: tôi đồng ý với chẩn đoán kỹ thuật, không đồng ý với quy mô đầu tư ngầm định trong cách trình bày 4 tài liệu.
