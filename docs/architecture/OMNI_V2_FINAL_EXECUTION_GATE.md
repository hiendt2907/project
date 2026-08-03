> **CTO SIGN-OFF AMENDMENT (chốt cuối, ghi đè các mục bị sửa bên dưới — đọc block này TRƯỚC khi đọc phần còn lại của tài liệu):**
> Ký duyệt roadmap này với 4 điều chỉnh so với bản gốc:
> 1. **Tách "MUST FIX BEFORE GA" thành 3 tier rõ ràng** — không xếp `#15`/`#21` ngang hàng 2 blocker thật. Lý do: Redis eviction (`#15`) chỉ trở thành vấn đề khi memory pressure thật xảy ra — bằng chứng `875MB/2GB` chứng minh XU HƯỚNG, chưa chứng minh imminent failure, khác hẳn `teardown-omni-postgres` (chỉ cần 1 lệnh sai là mất dữ liệu ngay). Tier mới: **Blocker** (`#12`, `#14`) → **High Priority** (`#21` blast_radius timeout, `#15` Redis audit storage) → **Medium** (`#13` backup, WS5 `#6`).
> 2. **WS5 là milestone độc lập, KHÔNG được commit xen lẫn task khác.** `omni_worker.py`→Capability Registry không phải "1 refactor" — nó đổi composition root, dependency graph, startup sequence, lifecycle, và ownership của mọi background loop cùng lúc. Đây là thay đổi lớn nhất toàn roadmap, cần PR/commit boundary riêng.
> 3. **Build Order đổi**: WS5 làm NGAY SAU WS0/WS1 (dọn nền tảng), TRƯỚC `#15`/`#21`/`#13` — vì WS5 đụng gần như toàn bộ dependency graph; làm các fix cục bộ (Redis/timeout) trước rồi mới WS5 sẽ tăng rủi ro conflict. Xem Implementation Order đã sửa ở cuối tài liệu.
> 4. **Scope Freeze bổ sung 1 dòng còn thiếu**: *"Không tạo thêm capability mới trong quá trình implement. Nếu phát hiện nhu cầu mới: ghi ADR, mở issue, không mở rộng scope của WS hiện tại."* — chặn hiện tượng "tiện thể thêm.../tiện thể refactor..." khiến roadmap phình trở lại sau tuần thứ 2.
>
> Ghi nhận thêm (không phải action item, chỉ là nhận định giữ lại cho lịch sử): sau 4 vòng phản biện, roadmap đã chuyển từ *architecture-driven* sang *incident-driven* — mọi hạng mục còn giữ lại đều trace được về ít nhất 1 trong 3 loại bằng chứng (incident đã xảy ra / lỗi verify được bằng code-runtime / invariant sản phẩm đã tồn tại). Đây là tín hiệu chất lượng backlog quan trọng hơn số lượng finding.
>
> **Verdict cuối cùng, không đổi: APPROVED WITH BLOCKERS (2). Architecture Freeze: có hiệu lực. Bắt đầu coding: có.**

# Omni v2 — FINAL EXECUTION GATE (Architecture Freeze)

**Vai trò:** CTO ký duyệt cho đội engineering bắt đầu code. Không tìm thêm issue, không thêm capability/context/WS/module/abstraction. Chỉ trả lời 4 câu hỏi và khoá kiến trúc.
**Input:** `OMNI_V2_ARCHITECTURE_REDESIGN.md`, `OMNI_V2_IMPLEMENTATION_PLAN.md`, `OMNI_V2_RED_TEAM_REVIEW.md`, `OMNI_V2_RED_TEAM_ROUND2_REVIEW.md`, `OMNI_V2_FINAL_SHIP_REVIEW.md`.
**Hành động phát sinh trong lượt này** (không phải finding mới — chỉ là sửa lỗi vận hành của chính hệ thống task tracking): phát hiện `TaskUpdate.addBlockedBy` chỉ CỘNG THÊM, không xoá được — 3 task (`#3`, `#7`, `#9`) vẫn hiện `blocked by` những task đã bị Final Ship Review hạ mức (`#16`, `#20`), dù ý định là gỡ. Đã sửa bằng cách ghi rõ trong description "coi blockedBy này là stale, không phải điều kiện chặn thật" (công cụ không hỗ trợ xoá field). Đây là 1 dependency-tracking bug thật, đã đóng.

---

# Question 1 — Kiến trúc nào là FINAL, không còn tranh cãi

**Component (đã khoá):**
- 5 plane: Control Plane (`gateway/`), Knowledge Plane (Observation/Baseline&Anomaly/Knowledge/Reasoning), Execution Plane (Execution/Verification), Agent Plane (`remote_agent/`), Data Plane (Postgres/Redis/Kafka).
- Governance = `pkg/executor/mutate_governance.py` (permission tĩnh). Autonomy = `pkg/autonomy/tier_gate.py` + `ConfidenceLevel` (authority động). **Giữ 2 module riêng, KHÔNG có Engine/Control-Plane class mới bọc quanh chúng.**
- `pkg/autonomy/gate.py` + `policy.py` — code chết, xoá hẳn.
- `omni_worker.py` → Capability Registry (composition root ~200 dòng, không chứa business logic domain).
- CRAT (`services/audit_ledger/`) — schema/hash-chain giữ nguyên 100%, chỉ đổi storage policy (Redis eviction).
- System Twin (`aoip.SystemModel`) — giữ nguyên kiến trúc, không "promote". `blast_radius.py` chỉ thêm nhánh union-mở-rộng (khi triển khai, hiện hoãn).

**Ownership (đã khoá, 1 owner/entity):** Decision → CRAT (`chain_writer.py`, nguồn ghi chính) + trace-stage (tiến độ, không phải nguồn quyết định) + Kafka `omni-actions` (transport, không phải store) — 3 nơi nhưng vai trò khác nhau rõ ràng, không tranh chấp "ai đúng" (đã giải quyết ở Final Ship Review: CRAT ghi trước Kafka publish là đúng ngữ nghĩa, không phải lỗi). Confidence → `remote_host_baseline.py`. Blast Radius → `blast_radius.py` (K8s live rule là sàn bắt buộc, Twin chỉ mở rộng). Fact/Twin → `aoip.system_model_store.py`.

**Dependency (đã khoá):** `gateway/` không import `workers/` (giữ nguyên, đã đúng). `workers/` → `pkg/`, `services/`, `anomaly/`, `rag/` (đúng chiều). `pkg/`/`anomaly/`/`rag/` **không được** import ngược `workers/` — thực thi bằng `import-linter` (WS0, sau khi WS1 sửa xong).

**Workstream đã khoá để BUILD NGAY** (xem Question 3 — 2 WS đã bị loại khỏi roadmap chủ động ở lượt này): WS0, WS1, WS2 (thu nhỏ), WS5, cộng 5 task must-fix (`#12`, `#13`, `#14`, `#15`, `#21`).

**Còn tranh cãi — GHI RÕ (duy nhất còn lại, không phải kiến trúc mà là tham số/quy trình cụ thể, xem Question 4):** cơ chế chính xác để tách `audit_chain:*` khỏi `allkeys-lru` (3 phương án tương đương chưa chọn 1); giá trị timeout cụ thể cho `blast_radius.py` K8s call; quy trình phân phối/xoay public key Ed25519 khi WS4 (Ed25519 signing) thực sự triển khai.

---

# Question 2 — Mâu thuẫn / duplicate / WS chồng nhau / dependency sai / rollback / migration

- **Mâu thuẫn:** không còn (Final Ship Review đã giải quyết toàn bộ mâu thuẫn severity giữa Round 1/2).
- **Duplicate:** không còn (`#17` evidence_consumer.py và WS5 omni_worker.py là 2 file khác nhau, không trùng).
- **WS chồng nhau:** không còn sau khi loại WS3+WS8 (Question 3) — 2 WS này là nơi duy nhất có overlap tiềm ẩn (cùng thêm bảng Postgres cho mục đích gần nhau).
- **Dependency sai:** **CÓ, đã tìm thấy và sửa trong lượt này** — `#3`/`#7`/`#9` giữ `blockedBy` trỏ tới `#16`/`#20` dù Final Ship Review đã quyết định gỡ (công cụ task không xoá được field, đã note rõ trong description từng task — xem đầu tài liệu). Sau khi loại `#9` (Question 3), vấn đề này chỉ còn ảnh hưởng `#3` (stale `blocked by #16`, đã note) và `#7` (stale `blocked by #20`, đã note, nhưng `#7` cũng không còn trong tập BUILD NGAY nên không cấp bách).
- **Rollback không khả thi:** không còn — WS2 sau khi bị Final Ship Review thu nhỏ (chỉ xoá code chết + thêm 1 CRAT event, không còn consolidation logic) **không cần feature flag nữa** (Round 1 từng yêu cầu `OMNI_USE_LEGACY_AUTONOMY_CHECKS` — không còn cần thiết vì không có behavior thay thế, chỉ có xoá + thêm thuần tuý, rollback = `git revert`). Mọi task còn lại trong tập BUILD NGAY (`#12/#13/#14/#15/#21`, WS0/WS1/WS5) đều rollback bằng revert commit hoặc revert image tag — đã xác nhận khả thi.
- **Migration không khả thi:** **VERIFIED — không còn migration Postgres nào trong tập BUILD NGAY** sau khi loại WS3 (`trace_evidence_archive`) và WS8 (`change_event`) khỏi roadmap chủ động (Question 3). `#13` (backup/restore) là chiến lược vận hành (pg_dump/CronJob), không phải schema migration.

**Kết luận Question 2:** VERIFIED sau khi sửa 1 dependency-tracking bug thật (đã sửa trong lượt này).

---

# Question 3 — WS nào không tạo value / không giảm risk / không sửa incident thật

Áp dụng nghiêm ngặt: mỗi WS phải trace được về (a) incident thật đã xảy ra, (b) rủi ro đã verify bằng bằng chứng sống, hoặc (c) nguyên tắc sản phẩm đã cam kết (invariant có sẵn trong CLAUDE.md), không phải "sẽ tốt hơn".

| WS | Có incident/risk/invariant thật? | Quyết định |
|---|---|---|
| WS0/WS1 | CÓ — circular dependency thật, đã gây khó bảo trì xác nhận | GIỮ |
| WS2 (thu nhỏ) | CÓ — code chết 369 dòng gây nhầm lẫn thật (2 hệ song song) | GIỮ |
| **WS3 (Operational Memory)** | **KHÔNG** — chính Round 1 tự nhận "chưa có bằng chứng operator nào thực sự cần replay ngoài đọc log/CRAT thủ công" | **REMOVE khỏi roadmap chủ động** — đã xoá task `#4` khỏi tracker, ý tưởng giữ trong `OMNI_V2_RED_TEAM_REVIEW.md` mục 6 làm tài liệu tham khảo, không phải cam kết xây |
| WS4 (chỉ phần Ed25519 + version-gate) | CÓ — supply-chain risk thật đã verify (checksum do chính API caller cấp, không có chữ ký độc lập) | GIỮ, nhưng KHÔNG trong tập BUILD NGAY (ưu tiên bảo mật cao nhưng không phải BLOCKER/MUST-FIX-GA) |
| WS5 | CÓ — crash-loop production thật đã xảy ra (Đ12) | GIỮ, BUILD NGAY |
| WS6 | CÓ — blast-radius bảo mật thật (LLM tool-call chung ServiceAccount với quyền mutate, verify được) | GIỮ, hoãn sau GA |
| WS7 | CÓ — blast_radius.py xác nhận không dùng dữ liệu sẵn có (verify code), dù chưa gây incident | GIỮ, hoãn sau GA |
| **WS8 (Change context)** | **KHÔNG trực tiếp** — insight đúng (gap thật: "không trả lời được cái gì đổi trước khi hỏng") nhưng không có incident cụ thể nào bị bỏ lỡ vì thiếu nó, giá trị hoàn toàn suy đoán (giảm MTTR chưa đo được) | **REMOVE khỏi roadmap chủ động** — đã xoá task `#9`, ý tưởng giữ trong `OMNI_V2_RED_TEAM_REVIEW.md` mục 7 |
| WS9 | CÓ — vi phạm trực tiếp invariant sản phẩm đã cam kết ("agent đề xuất, Omni quyết", ghi trong CLAUDE.md, hiện 5/6 domain vi phạm) | GIỮ, hoãn sau GA |
| `#12`/`#14`/`#15`/`#21` | CÓ — mỗi cái đều verify bằng bằng chứng sống (script/redis-cli/code đọc trực tiếp) | GIỮ, BUILD NGAY |
| `#13` | CÓ — không có backup là rủi ro thật cho dữ liệu load-bearing đã tồn tại | GIỮ, BUILD NGAY |
| `#16`/`#17`/`#18`/`#19`/`#20` | Rủi ro thật nhưng không cấp bách (đã hạ mức Final Ship Review) | GIỮ làm ghi chú rủi ro đã biết, không phải WS chủ động |

**Kết luận Question 3:** 2 WS bị loại (WS3, WS8) — không tạo value đo được, không sửa incident thật. Toàn bộ WS còn lại đều trace được về bằng chứng cụ thể.

---

# Question 4 — Team mới có implement liên tục mà không cần hỏi lại kiến trúc sư không?

**Với tập BUILD NGAY (WS0/WS1/WS2/WS5 + 5 must-fix): CÓ**, đủ chi tiết (file cụ thể, hàm cụ thể, pattern mẫu đã chỉ ra trong code — ví dụ `#14` chỉ cần mirror `_apply_plan_ceiling()` cùng file).

**3 decision còn thiếu** (không redesign — chỉ liệt kê, cần 1 quyết định ngắn từ kiến trúc sư hoặc engineer lead trước khi code đúng 3 điểm này):

1. **Cơ chế cụ thể để cách ly `audit_chain:*` khỏi `allkeys-lru`** (`#15`) — 3 phương án đã nêu (đổi policy scoped/logical DB riêng/instance riêng) nhưng chưa chọn 1. Engineer cần 1 quyết định (khuyến nghị: logical DB riêng trong CÙNG Redis instance là rẻ nhất, không cần hạ tầng mới — nhưng đây là gợi ý, không phải quyết định khoá).
2. **Giá trị timeout cụ thể cho `blast_radius.py` K8s call** (`#21`) — chưa có con số (ví dụ 5s/10s/30s). Cần 1 quyết định trước khi code, không phải kiến trúc.
3. **Quy trình phân phối/xoay public key Ed25519** cho WS4 (khi thực sự triển khai, không phải BUILD NGAY) — thiết kế mới chỉ nói "pinned cứng trong bundle agent" nhưng chưa nói rotate key thế nào nếu private key Omni bị lộ. Không cần quyết định NGAY (WS4 không trong tập BUILD NGAY) nhưng phải quyết trước khi WS4 bắt đầu.

Không có decision nào khác còn thiếu cho tập BUILD NGAY.

---

# OUTPUT

## 1. FINAL VERDICT

**APPROVED WITH BLOCKERS**

## 2. Remaining Blockers (tối đa 5)

1. `#12` — Trung hoà landmine `teardown-omni-postgres` (script + Makefile).
2. `#14` — Fix `resolve_tier()` fail-closed khi Redis mất kết nối (effort ~1 giờ).

(Chỉ 2 — không có blocker thứ 3, 4, 5.)

## 3. Locked Architecture

- 5 plane (Control/Knowledge/Execution/Agent/Data), không đổi.
- Governance (`mutate_governance.py`) và Autonomy (`tier_gate.py`) là 2 module vĩnh viễn tách biệt — **không bao giờ** bọc bằng 1 "Control Plane"/"Engine" class.
- `pkg/autonomy/gate.py`+`policy.py` — xoá, không hồi sinh.
- Capability Registry thay `omni_worker.py` — composition root, cấm chứa business logic domain.
- CRAT schema/hash-chain — không đổi, chỉ đổi storage policy.
- System Twin — không "promote", chỉ thêm nhánh union khi WS7 thực sự triển khai (sau GA).
- Dependency rule: `pkg/`/`anomaly/`/`rag/` không import `workers/`, thực thi bằng `import-linter`.

## 4. Locked Roadmap

**LÀM (BUILD NGAY, trước GA):** WS0, WS1, WS2 (thu nhỏ), WS5, `#12`, `#13`, `#14`, `#15`, `#21`.
**LÀM SAU GA (đã lên lịch, không phải bây giờ):** WS4 (chỉ Ed25519+version-gate), WS6, WS7, WS9.
**BỎ khỏi roadmap chủ động (Question 3):** WS3 (Operational Memory), WS8 (Change context) — ý tưởng giữ trong tài liệu, không phải cam kết.
**GHI NHẬN RỦI RO, không phải WS** (đã hạ mức Final Ship Review, backlog thuần): `#16` (Kafka partition-key), `#17` (`evidence_consumer.py`), `#18` (Redis/Kafka HA), `#19` (LLM capacity), `#20` (leader-election).
**MERGE:** không có WS nào cần merge (đã merge WS10 vào dọn dẹp chung ở Final Ship Review).

## 5. Engineering Ready

| Tiêu chí | Điểm |
|---|---|
| Architecture | 85% |
| Implementation Plan | 80% (tập BUILD NGAY); 40% (tập LÀM SAU GA, còn 3 decision treo — Question 4) |
| Migration | 95% (không còn schema migration nào trong tập BUILD NGAY) |
| Rollback | 90% (mọi task BUILD NGAY revert bằng commit/image-tag, không cần feature flag) |
| Testing | 75% (7348 test làm lưới an toàn hồi quy, nhưng 0 test mới đã viết cho các fix cụ thể — implementation = 0%) |
| Production Safety | 65% (2 blocker + 5 must-fix giải quyết đúng rủi ro thật đã verify; risk đã biết còn lại — Redis/Kafka HA, LLM capacity — được CHẤP NHẬN có kiểm soát cho tier Enterprise Pilot, không phải bị bỏ qua vô thức) |

## 6. Implementation Order (ĐÃ SỬA theo CTO Amendment — thay thế bản gốc)

```
#14 — BLOCKER — resolve_tier fail-closed (trivial, làm trước tiên)
  ↓
#12 — BLOCKER — trung hoà landmine Postgres
  ↓
WS1 (#2) — sửa 5 import ngược
  ↓
WS0 (#1) — import-linter, khoá lại bằng công cụ
  ↓
WS5 (#6) — MEDIUM nhưng làm NGAY ở đây, MILESTONE ĐỘC LẬP
            (đụng gần như toàn bộ dependency graph — làm trước để các fix
            cục bộ phía sau không conflict; PR/commit riêng, không trộn)
  ↓
#21 — HIGH PRIORITY — timeout blast_radius K8s call
  ↓
#15 — HIGH PRIORITY — Redis eviction policy cho CRAT
  ↓
#13 — MEDIUM — backup/restore Postgres
  ↓
WS2 (#3) — MUST FIX BEFORE GA — xoá code chết + DECISION_RENDERED
  ↓
=== GA (Enterprise Pilot) ===
  ↓
WS4 (#5, Ed25519 signing) → WS6 (#7, plane split) → WS7 (#8, Twin→blast-radius) → WS9 (#10, Remote Agent SDK)
   (thứ tự trong nhóm này linh hoạt theo tín hiệu khách hàng thật, không cố định)
```

**Tier chốt cuối (thay thế phân loại 4-tier gốc cho các task bị amendment):**

| Tier | Task |
|---|---|
| **Blocker** | `#12` teardown-postgres, `#14` resolve_tier fail-closed |
| **High Priority** | `#21` blast_radius timeout, `#15` Redis audit storage |
| **Medium** | `#13` backup, `#6` WS5 (milestone độc lập, effort lớn nhất roadmap dù tier "chỉ" Medium về mức khẩn cấp) |
| MUST FIX BEFORE GA (không đổi) | WS0 (`#1`), WS1 (`#2`), WS2 (`#3`) |

## 7. Scope Freeze (bổ sung theo CTO Amendment)

> Không tạo thêm capability mới trong quá trình implement.
> Nếu phát hiện nhu cầu mới:
> - ghi ADR
> - mở issue
> - không mở rộng scope của WS hiện tại.

Áp dụng cho toàn bộ Implementation Order ở trên — bất kỳ ai code WS0-WS9/`#12-21` phát hiện "tiện
thể sửa thêm X" phải dừng, ghi ADR/issue riêng, KHÔNG mở rộng WS đang làm.

## 8. Final Statement

**Có.**

---

## 9. Engineering Process Rules (bổ sung sau ký duyệt, không phải review kiến trúc mới)

Ngay sau khi verdict này được xác nhận, CTO yêu cầu ghi thêm 3 quy tắc vận hành trước khi
coding — không phải finding mới, không đổi kiến trúc/roadmap đã khoá ở trên:

1. **Definition of Done bắt buộc cho mọi WS/must-fix** (code merged / unit test / integration
   test / docs updated / rollout verified / rollback verified) — cụ thể hoá theo từng task.
2. **Một PR chỉ implement một WS hoặc một must-fix** — không trộn, kể cả các fix nhỏ "tiện thể".
3. **Freeze Acceptance Criteria**: không đổi Definition of Done của một WS sau khi đã có PR mở,
   trừ khi có ADR mới.

Chi tiết đầy đủ (bảng DoD theo từng task, ví dụ PR đúng/sai): **`ENGINEERING.md`**.

**Verdict kickoff cuối cùng:**

| Hạng mục | Trạng thái |
|---|---|
| Architecture | ✅ Freeze |
| Roadmap | ✅ Freeze |
| Engineering kickoff | ✅ Cho phép bắt đầu |
| Review kiến trúc tiếp theo | ❌ Dừng |

Từ đây, giá trị đến từ code review / integration review / production validation theo từng WS khi
triển khai thật — không phải thêm tài liệu kiến trúc.
