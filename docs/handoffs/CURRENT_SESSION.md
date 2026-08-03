# Current Session Handoff

**Cập nhật:** 2026-08-03 (Đ20 — IMPLEMENTATION MODE: TOÀN BỘ tập BUILD NOW (9 hạng mục) đã xong + commit + push. GA-readiness theo roadmap đã khoá đạt được.) · **Branch:** `main` · **HEAD:** `103a25e` (14 commit mới từ `8cf91cb`, tất cả đã push)

## 🎯 Đ20 — IMPLEMENTATION MODE — TOÀN BỘ BUILD NOW xong (9/9), đã commit+push

User chuyển vai trò sang Senior Staff/Production/Refactoring Engineer, Architecture Frozen có
hiệu lực, yêu cầu code đúng Implementation Order đã khoá, mỗi task 1 commit độc lập, sau đó
"làm toàn bộ các task kia luôn rồi mới review lại". Đã hoàn tất **toàn bộ 9 hạng mục BUILD NOW**
theo đúng thứ tự khoá trong `OMNI_V2_FINAL_EXECUTION_GATE.md` mục 6:

`#14 → #12 → WS1(#2) → WS0(#1) → WS5(#6) → #21 → #15 → #13 → WS2(#3)`

**14 commit mới, tất cả đã push lên `origin/main`** (không còn gì ở working tree ngoài
`docs/handoffs/CURRENT_SESSION.md` đang sửa). Full test suite xanh sau MỌI commit (7313-7357
passed tuỳ thời điểm, 0 failed xuyên suốt). `make lint-imports`: 2 kept, 0 broken xuyên suốt.

### Tóm tắt từng hạng mục (chi tiết đầy đủ nằm trong message của từng commit)

- **`#14` BLOCKER — `resolve_tier()` fail-closed** (`19d2af8`): phát hiện khi đọc code thật —
  gap thật KHÔNG phải ở nhánh Redis (đã fail-closed từ trước) mà ở nhánh Postgres
  (`repo.get_tier()` không try/except). Sửa 5 dòng, mirror pattern có sẵn.
- **`#12` BLOCKER — landmine `teardown-omni-postgres`** (`025b1c7`): thêm guard kiểm tra
  `omni_admin.agent_credential` tồn tại trước khi cho `--apply`, cần thêm `--force-data-loss`
  mới ghi đè. Verify SỐNG: `--apply` abort đúng thiết kế trên cluster lab thật.
- **WS1 (`#2`) MUST FIX BEFORE GA — 7 import ngược `pkg/anomaly/rag → workers`** (`e3cd472`):
  move-symbol-xuống-pkg (giữ workers/ re-export) hoặc dependency-injection tuỳ độ gắn kết. Phát
  hiện thêm `pkg/rag/gate.py`/`pkg/reasoning/diagnostic_policy.py` cũng vi phạm nhưng NGOÀI
  scope task — ghi rõ, không tự sửa (đã sửa sau ở `2cd5d5b` khi WS0 lộ ra qua tool thật).
- **WS0 (`#1`) MUST FIX BEFORE GA — wire `import-linter`** (`2cd5d5b` + `a92f46a`): chạy tool
  lần đầu lộ thêm 6 vi phạm thật ngoài 7 điểm WS1 đã liệt kê thủ công — sửa 6/8, 2 điểm còn lại
  (`pkg.observability.llm_observability` soft-dependency có chủ đích, `pkg.reasoning.
  known_fix_resolver` cần DI refactor lớn hơn) ghi `ignore_imports` có giải thích rõ, không xoá
  ngầm. Wiring: `.importlinter`, `Makefile lint-imports`, `.pre-commit-config.yaml` (cơ chế CI
  thực tế DUY NHẤT của repo — xác nhận KHÔNG có `.github/workflows` trước khi viết).
- **WS5 (`#6`) MILESTONE ĐỘC LẬP — Capability Registry** (`68f04c9`, 1 commit riêng đúng yêu
  cầu CTO): tách `_worker_background_tasks` (100 dòng if/else — đúng lớp bug gây crash-loop
  Đ12) thành 5 hàm `_register_*_capability()` độc lập theo bounded context. **Phạm vi phiên
  này CHỈ tách hàm dispatch, KHÔNG di chuyển vật lý ~1100 dòng thân loop** — đọc đúng lời văn
  đã khoá trong Final Execution Gate CTO Amendment (ưu tiên cao hơn Implementation Plan gốc
  vốn mô tả tham vọng hơn "~200 dòng"). Verify: đếm task name registered trước/sau khớp 100%,
  126 test có sẵn bao phủ đúng hàm này pass.
- **`#21` HIGH PRIORITY — timeout+circuit-breaker `blast_radius.py`** (`b20ca3d`):
  `K8S_API_TIMEOUT_SEC=10s` qua `asyncio.wait_for`; circuit breaker CẤP MODULE (không phải
  per-instance, vì reader tạo mới mỗi lần mutate) mở sau 3 lỗi liên tiếp, cooldown 30s. 8 test
  mới.
- **`#15` HIGH PRIORITY — Redis `audit_chain:*` khỏi eviction** (`5d9fc35`): **hỏi lại user** vì
  phương án tài liệu gợi ý ("logical DB riêng") không thực sự cô lập được (maxmemory-policy là
  instance-wide, không tách theo DB) — user chọn đổi policy global `allkeys-lru`→`volatile-lru`.
  Audit AN TOÀN trước khi đổi (như đã hứa): quét toàn bộ Redis `.set()` không TTL, tìm đúng 2
  rủi ro thật (đã tạo task `#22`/`#23`, KHÔNG tự sửa). Deploy sống + verify TTL=-1 trên
  `audit_chain:*` thật.
- **`#13` MEDIUM — backup/restore `omni-postgres`** (`42db83d`): CronJob `pg_dump` hằng ngày +
  PVC riêng + `scripts/restore_omni_postgres.sh` (an toàn — target mặc định DB throwaway, restore
  vào DB thật cần 2 flag xác nhận). Verify round-trip THẬT trên cluster lab: backup → restore →
  so khớp 32/32 bảng + 3/3 dòng `agent_credential` giữa nguồn và đích.
- **WS2 (`#3`) MUST FIX BEFORE GA — Decision Transparency Layer** (`103a25e`): xoá hẳn
  `pkg/autonomy/gate.py`+`policy.py` (code chết, dùng lane trục A đã bỏ 2026-07-30) sau khi xác
  nhận 0 call site production thật; xoá 4 endpoint `/autonomy/policy*` sau khi xác nhận consumer
  HTTP duy nhất (`ui/` root) đã RETIRED, không phải portal đang chạy thật. Thêm CRAT event
  `DECISION_RENDERED` — 1 record duy nhất giải thích ALLOW/DENY cho mỗi `EXECUTE_MUTATE`, best-
  effort. Xoá/sửa 10 file test tham chiếu module đã xoá, thêm 2 test mới cho event mới.

### Quyết định cần chú ý khi review lại

1. **WS5 scope**: chỉ tách dispatch logic, chưa di chuyển vật lý phần thân loop (~1100 dòng còn
   nằm trong `omni_worker.py`). Nếu user muốn đúng "~200 dòng" như Implementation Plan gốc mô
   tả, đây là việc CÒN LẠI, effort lớn hơn nhiều, nên là 1 milestone riêng tiếp theo.
2. **`#15`**: đã đổi GLOBAL Redis policy — không chỉ audit_chain, MỌI key không TTL trên Redis
   giờ không bao giờ bị evict. Đã audit và tạo `#22`/`#23` cho 2 rủi ro thật tìm được
   (discovery_doc.py diagram version, mission_store.py mission) — CHƯA sửa, chỉ ghi nhận.
3. **`#22`/`#23`** (task mới tạo trong lượt này): unbounded-growth risk phát sinh TRỰC TIẾP từ
   quyết định `#15` — nên xem xét sớm, không phải "nice to have" xa vời.
4. Không còn Blocker/High Priority/Medium/Must-Fix-Before-GA nào TỒN ĐỌNG trong tập BUILD NOW.

### Còn lại trong roadmap (SAU GA — CAN FIX AFTER GA / NICE TO HAVE, CHƯA đụng tới)

`WS4(#5)` Ed25519 signing · `WS6(#7)` Execution/Knowledge Plane split · `WS7(#8)` System Twin →
blast-radius · `WS9(#10)` Remote Agent SDK OBSERVED-only — theo đúng Implementation Order, các
hạng mục này nằm SAU mốc `[GA]`, thứ tự linh hoạt theo tín hiệu khách hàng thật, KHÔNG cố định.
Chưa bắt đầu bất kỳ hạng mục nào trong nhóm này — đang chờ xác nhận từ user có muốn tiếp tục hay
dừng lại review như đã hẹn ("làm toàn bộ rồi mới review lại" — toàn bộ BUILD NOW đã xong, đây là
điểm dừng tự nhiên để review).

## 🎯 (Đ20 gốc, giữ lại tham khảo) — Task `#14` DONE (code, chưa commit)

User chuyển vai trò từ CTO/Architect sang **Senior Staff/Production/Refactoring Engineer**,
Architecture Frozen chính thức có hiệu lực. Nguồn sự thật: `OMNI_V2_FINAL_EXECUTION_GATE.md` >
`OMNI_V2_FINAL_SHIP_REVIEW.md` > `OMNI_V2_IMPLEMENTATION_PLAN.md`. Quy tắc: 1 PR = 1 WS/must-fix,
không trộn milestone, đọc code thật trước khi sửa (không suy đoán), đổi tối thiểu, nếu phát hiện ý
tưởng mới → ADR/issue chứ không implement, nếu thiếu thông tin → dừng và hỏi đúng 1 câu.

**Task `#14` (`[BLOCKER 2/2] resolve_tier() không fail-closed`) — DONE, code xong, test xanh, CHƯA
commit.**

**Phát hiện khi đọc code thật (đúng quy tắc "không suy đoán")**: mô tả gốc của `#14` ghi "không
fail-closed khi Redis mất kết nối", nhưng `read_tier_cached()` (`services/admin_config/cache.py`)
ĐÃ có try/except fail-closed từ commit `05222f8` (22/06), có trước session review này. Gap thật
nằm ở nhánh Postgres trong `resolve_tier()` (`src/pkg/autonomy/tier_gate.py` dòng ~177-180):
`repo.get_tier(tenant_id)` không được bọc try/except, khác `_apply_plan_ceiling()` cùng file (đã
đúng pattern). Đây vẫn là đúng ý `#14` ("resolve_tier phải luôn fail-closed"), chỉ chính xác hoá
dòng cần sửa dựa trên code thật — không phải ý tưởng mới, không cần ADR/issue.

**Fix**: bọc `repo.get_tier(tenant_id)` trong try/except, log warning, return `SHADOW` khi lỗi —
mirror chính xác pattern `_apply_plan_ceiling()` đã có (+5 dòng, đổi tối thiểu).

**Files changed (task `#14`, CHƯA commit)**:
- `src/pkg/autonomy/tier_gate.py` — try/except quanh `repo.get_tier()`, fail-closed `SHADOW`.
- `tests/test_tier_gate_and_hitl.py` — thêm `_ExplodingRepo` + test
  `test_resolve_tier_db_lookup_fail_closed` (repo raise `ConnectionError`, assert = `shadow` dù
  env tier = `auto`, chứng minh fail-closed thật chứ không phải trùng hợp).

**Test**: `pytest tests/test_tier_gate_and_hitl.py` 38 passed · `pytest
tests/test_autonomy_tier_endpoint.py tests/test_gateway_agent_runtime.py` (2 caller khác của
`resolve_tier`) 35 passed. Không regression.

**Rollback**: `git revert` đơn thuần, không state/migration.

**Commit message đề xuất** (CHƯA chạy `git commit` — chờ user xác nhận theo đúng convention "GIT
chỉ khi được chỉ thị"):
```
fix(autonomy): resolve_tier() fail-closed khi Postgres lookup lỗi

repo.get_tier() không được bọc try/except, khác với _apply_plan_ceiling()
cùng file — Postgres mất kết nối làm resolve_tier() raise thay vì trả
tier hợp lệ. Mirror pattern try/except đã có, fail-closed về shadow.
```

**Next**: `#12` (landmine `teardown-omni-postgres`) — chờ user xác nhận có commit `#14` trước hay
không rồi mới chuyển sang `#12` theo đúng Implementation Order đã khoá.

## 🎯 Đ19 (chốt cuối) — ENGINEERING.md: 3 quy tắc vận hành trước khi coding

User (vai CTO) xác nhận lại verdict Đ19 (Architecture ✅ Freeze / Roadmap ✅ Freeze / Engineering
kickoff ✅ / Review kiến trúc tiếp ❌ Dừng) và yêu cầu ghi thêm 3 quy tắc vào **`docs/architecture/
ENGINEERING.md`** (file mới) trước khi code:
- **A. Definition of Done bắt buộc** cho từng WS/must-fix — không chỉ checklist chung, mà bảng cụ
  thể "rollout verified nghĩa là gì / rollback verified nghĩa là gì" cho từng task trong
  Implementation Order (`#14`, `#12`, WS1, WS0, WS5, `#21`, `#15`, `#13`, WS2).
- **B. Một PR chỉ implement một WS hoặc một must-fix** — không trộn, kể cả fix nhỏ "tiện thể".
- **C. Freeze Acceptance Criteria** — không đổi Definition of Done của 1 WS sau khi đã có PR mở,
  trừ khi có ADR mới (hệ quả trực tiếp của Scope Freeze đã ghi ở Đ19 gốc).

Đã thêm mục "9. Engineering Process Rules" vào cuối `OMNI_V2_FINAL_EXECUTION_GATE.md` trỏ sang
`ENGINEERING.md`, ghi lại bảng verdict kickoff. **Đây là điểm dừng thật sự của toàn bộ chuỗi review
Đ13-Đ19+amendment** — không mở thêm vòng review kiến trúc nữa theo đúng nguyên tắc user đặt ra.
Lượt kế tiếp: code `#14` (`resolve_tier` fail-closed), PR riêng, đối chiếu DoD trong
`ENGINEERING.md` mục A trước khi coi là xong.

## 🎯 Đ19 — FINAL EXECUTION GATE (Architecture Freeze — đóng chuỗi review Đ13-Đ19)

> **CTO SIGN-OFF AMENDMENT (chốt cuối cùng, user duyệt với 4 điều chỉnh — KHÔNG phải vòng review
> mới, đây là bản chốt thật của Đ19):**
> 1. **`#15`/`#21` KHÔNG ngang hàng blocker** — hạ xuống tier **High Priority** riêng (Redis
>    eviction mới chỉ chứng minh xu hướng 875MB/2GB, chưa chứng minh imminent failure — khác hẳn
>    `teardown-postgres` chỉ cần 1 lệnh sai là mất dữ liệu ngay). `#13`/WS5(`#6`) → tier **Medium**.
>    Tier cuối: **Blocker** (`#12`,`#14`) → **High Priority** (`#21`,`#15`) → **Medium** (`#13`,`#6`).
> 2. **WS5 (`#6`) là milestone ĐỘC LẬP** — không được commit xen lẫn task khác. Lý do: đổi
>    composition root + dependency graph + startup + lifecycle + ownership của mọi loop cùng lúc —
>    lớn nhất toàn roadmap dù tier khẩn cấp chỉ "Medium".
> 3. **Build Order đổi**: WS5 làm NGAY SAU WS0/WS1, TRƯỚC `#21`/`#15`/`#13` (không phải sau như bản
>    gốc) — vì WS5 đụng gần toàn bộ dependency graph, làm fix cục bộ trước rồi mới WS5 tăng rủi ro
>    conflict. Order mới: `#14 → #12 → WS1(#2) → WS0(#1) → WS5(#6, milestone riêng) → #21 → #15 →
>    #13 → WS2(#3) → [GA] → WS4→WS6→WS7→WS9`.
> 4. **Thêm dòng Scope Freeze còn thiếu**: *"Không tạo thêm capability mới trong quá trình
>    implement. Nếu phát hiện nhu cầu mới: ghi ADR, mở issue, không mở rộng scope của WS hiện
>    tại."* — chặn hiện tượng "tiện thể thêm/tiện thể refactor" khiến roadmap phình lại sau tuần 2.
>
> User ghi nhận: sau 4 vòng phản biện, roadmap đã chuyển từ *architecture-driven* sang
> *incident-driven* — mọi hạng mục còn lại trace được về ≥1 trong 3 bằng chứng (incident thật/lỗi
> verify được bằng code-runtime/invariant sản phẩm đã tồn tại). **Verdict không đổi: APPROVED WITH
> BLOCKERS (2). Bắt đầu coding: có.** Đã cập nhật `docs/architecture/OMNI_V2_FINAL_EXECUTION_GATE.md`
> (thêm block amendment đầu file + sửa mục 6 Implementation Order + thêm mục 7 Scope Freeze) và 4
> task (`#15`,`#21`,`#13`,`#6` — subject/description phản ánh tier + build-order mới).


User giao vai CTO ký duyệt cuối cùng, chỉ trả lời đúng 4 câu hỏi (không tìm thêm issue, không
thêm capability/context/WS/module) rồi khoá kiến trúc. **Deliverable:**
`docs/architecture/OMNI_V2_FINAL_EXECUTION_GATE.md`.

**Phát hiện vận hành trong lượt này (không phải finding kiến trúc)**: `TaskUpdate.addBlockedBy`
chỉ CỘNG THÊM, không xoá được field — 3 task (`#3`/`#7`/`#9`) vẫn hiện `blocked by` những task đã
bị Đ18 hạ mức (`#16`/`#20`) dù ý định là gỡ. Đã sửa bằng cách ghi rõ trong description "coi
blockedBy này là stale" (công cụ không hỗ trợ xoá field) — đây là "dependency sai" thật mà
Question 2 hỏi tới, đã tìm và đóng ngay trong lượt này.

**Quyết định quan trọng nhất: LOẠI 2 WORKSTREAM khỏi roadmap chủ động (Question 3)** — áp dụng
nghiêm ngặt tiêu chí "phải trace được về incident thật/risk đã verify/invariant đã cam kết":
- **WS3 (Operational Memory) — REMOVE.** Round 1 tự nhận "chưa có bằng chứng operator nào thực sự
  cần replay ngoài đọc log/CRAT thủ công" — giá trị hoàn toàn suy đoán. Đã xoá task `#4`.
- **WS8 (Change context) — REMOVE.** Insight đúng (gap thật) nhưng không có incident cụ thể nào
  bị bỏ lỡ vì thiếu nó — giá trị (giảm MTTR) chưa đo được. Đã xoá task `#9`.
- Ý tưởng của cả 2 vẫn giữ nguyên trong `OMNI_V2_RED_TEAM_REVIEW.md` (mục 6, 7) làm tài liệu tham
  khảo — không mất, chỉ không còn là cam kết xây trong roadmap hiện tại.

**Hệ quả tốt của việc loại WS3+WS8**: Question 2 (migration feasibility) đổi từ "cần xác nhận"
thành **VERIFIED tuyệt đối** — không còn schema Postgres migration nào trong tập BUILD NGAY (cả 2
bảng mới `trace_evidence_archive`/`change_event` đều thuộc 2 WS vừa bị loại). WS2 cũng được xác
nhận **không cần feature flag `OMNI_USE_LEGACY_AUTONOMY_CHECKS`** như Round 1 từng yêu cầu — vì
sau khi bị Đ18 thu nhỏ, WS2 chỉ còn xoá code chết + thêm 1 event, không có behavior thay thế nào
cần rollback dần — `git revert` thuần là đủ.

**Verdict: APPROVED WITH BLOCKERS.** Chỉ 2 blocker (giống Đ18, không đổi): `#12` (landmine
Postgres), `#14` (`resolve_tier` fail-closed). **3 decision còn thiếu** (Question 4, không phải
redesign — chỉ cần 1 quyết định ngắn trước khi code): (1) cơ chế cụ thể tách `audit_chain:*` khỏi
`allkeys-lru` (3 phương án đã nêu, chưa chọn); (2) giá trị timeout cụ thể cho `blast_radius.py`
K8s call (chưa có con số); (3) quy trình rotate public key Ed25519 cho WS4 (chỉ cần quyết trước
khi WS4 bắt đầu, không phải ngay).

**Implementation Order đã khoá (BẢN CHỐT CUỐI sau CTO amendment, xem block amendment ngay trên)**:
`#14 → #12 → WS1(#2) → WS0(#1) → WS5(#6, MILESTONE ĐỘC LẬP, không trộn commit) → #21 → #15 → #13
→ WS2(#3) → [GA] → WS4 → WS6 → WS7 → WS9`. Tier: Blocker (`#12`,`#14`) → High Priority
(`#21`,`#15`) → Medium (`#13`,`#6`).

**Engineering Ready**: Architecture 85% · Implementation Plan 80% (BUILD NGAY)/40% (sau GA) ·
Migration 95% · Rollback 90% · Testing 75% · Production Safety 65%.

**Final Statement: "Có nên dừng review và bắt đầu coding không?" → Có.** Đây là điểm dừng chính
thức của chuỗi review kiến trúc (Đ13-Đ19). Theo đúng nguyên tắc Architecture Freeze mà user đặt
ra: **không mở thêm vòng review kiến trúc mới** — mọi thay đổi tiếp theo phải xuất phát từ
implement/code-review/test/sự cố thực tế, không phải tiếp tục sửa tài liệu thiết kế.

**Task list sau lượt này: 18 task** (20 trừ `#4`, `#9` vừa xoá). Phiên sau chỉ cần đọc
`OMNI_V2_FINAL_EXECUTION_GATE.md` (không cần Đ13-Đ18 trừ khi cần chi tiết gốc) và bắt đầu từ `#14`
theo Implementation Order đã khoá.

**Chưa code gì trong lượt này** — đây là lượt cuối cùng thuần tài liệu của chuỗi review. Lượt kế
tiếp nên là code thật cho `#14`/`#12`.

## 🎯 Đ18 — FINAL SHIP REVIEW (hội đồng đầu tư: CTO+Architect+SRE+Enterprise Platform)

User giao vai hội đồng đầu tư cuối cùng, yêu cầu KHÔNG tìm thêm lỗi mà TRIAGE lại toàn bộ Round
1+2, trả lời đúng 1 câu: có ký duyệt roadmap này không. Được phép phủ định/hạ mức/nâng mức mọi
finding cũ.

**Deliverable:** `docs/architecture/OMNI_V2_FINAL_SHIP_REVIEW.md`.

**Verify mới bằng lệnh sống trên cluster (không chỉ đọc code):**
- `redis-0`: **1 pod duy nhất**, 35 restart/55 ngày, `used_memory=875.75M/maxmemory=2.00G` (44%
  đã dùng ở quy mô lab gần-zero tải thật) — NÂNG MỨC finding Redis-eviction (Đ17) từ lý thuyết
  thành có bằng chứng sống.
- `kafka-685dc55dfb-...`: **1 pod duy nhất**, 53 restart/81 ngày — xác nhận SPOF thật.
- `K8sBlastReader.__init__` (`blast_radius.py`): xác nhận KHÔNG có timeout nào set tường minh —
  giữ nguyên severity finding head-of-line-blocking (Đ17), không hạ.
- `src/services/siem_correlation/chain.py`: `sorted(members, key=lambda m: m.ts)` — sort theo
  field `ts` trong payload, KHÔNG phải thứ tự Kafka đến — **HẠ MỨC** finding "0 partition-key phá
  SIEM correlation" (Đ17) từ HIGH xuống LOW-MEDIUM, vì correlation đã tự chống chịu phần lớn.

**Kết quả Phase 1 (triage Round 1+2)**: nhiều finding Round 2 bị hạ mức sau khi verify lại kỹ hơn
— Decision "3 owner không đồng bộ" (CRAT ghi trước Kafka publish không thực sự sai ngữ nghĩa, chỉ
thiếu 1 event terminal-state); Kafka partition-key (đã nêu trên); leader-election "phải xong
trước WS6" (SAI — WS6 không đòi hỏi multi-replica, chỉ tách Deployment, vẫn `replicas: 1`);
`evidence_consumer.py` (3578 dòng, thật nhưng kích thước file không tự nó là outage risk); Policy
Compiler/Simulation/multi-region/billing (thật nhưng ngoài phạm vi roadmap hiện tại, không phải
thiếu sót cần chặn duyệt).

**Verdict: APPROVE WITH CONDITIONS.**
- **Chỉ 2 BLOCKER thật** (không được bắt đầu roadmap tiếp tục nếu chưa sửa): task #12 (landmine
  `teardown-omni-postgres`) và task #14 (`resolve_tier()` fail-closed, effort ~1 giờ).
- **MUST FIX BEFORE GA**: task #15 (Redis eviction policy cho CRAT — nâng mức bằng bằng chứng
  sống), #6 (WS5 god-object), #2+#1 (WS1→WS0 import cycle), #21 (blast_radius timeout), #13
  (backup Postgres), #3 (WS2, đã giảm yêu cầu).
- **CAN FIX AFTER GA**: #18 (Redis/Kafka HA — chặn Commercial SaaS, KHÔNG chặn Enterprise Pilot),
  #19 (LLM capacity — tương tự), #7 (WS6, gỡ `blockedBy` sai), #8 (WS7), #10 (WS9), #17
  (`evidence_consumer.py`), #16 (partition-key, hạ mức).
- **NICE TO HAVE**: #4 (WS3), #9 (WS8, gỡ `blockedBy` sai), #5 (WS4, riêng phần Ed25519 nên làm
  sớm vì bảo mật dù không blocking GA).
- **Đã xoá task #11 (WS10)** — "merge vào công việc dọn dẹp chung, không cần track như 1 WS riêng"
  theo đúng khuyến nghị Phase 4 (giảm ceremony).

**Phase 5 nói thẳng**: 4 tài liệu kiến trúc (~4000+ dòng) cho roadmap thực chất chỉ còn ~10-12
hạng mục kỹ thuật sau khi lọc — tỷ suất phát hiện-mới/trang-viết đã giảm mạnh ở vòng 4
(Round 2), phần lớn Phase 1 của Đ18 là XÁC NHẬN LẠI hoặc HẠ MỨC finding cũ, không phải phát hiện
mới. **Khuyến nghị: dừng vòng review kiến trúc tại đây, chuyển sang thực thi** — nếu cần review
tiếp, nên là code review sau khi implement, không phải thêm tài liệu kiến trúc.

**Phase 6 — đánh giá sản phẩm**: hoàn thành đúng roadmap đã lọc → Omni đạt **Enterprise Pilot**,
KHÔNG PHẢI Commercial SaaS (thiếu tenant resource isolation, billing/quota enforcement, Redis/
Kafka HA thật — dù bảng `tenant_plan_entitlements` đã tồn tại nhưng chưa nối vào enforcement nào).

**Estimated Readiness**: Architecture 75% · Implementation **0%** (xác nhận rõ: chưa có 1 dòng
code nào của bất kỳ WS nào được viết qua toàn bộ 5 vòng tài liệu Đ13-Đ18) · Production (v1 hiện
tại đang chạy) 55% · Commercial SaaS 15%.

**Final Statement (nguyên văn tinh thần)**: ký duyệt phần lõi (2 blocker + 5 must-fix-before-GA,
~6-8 tuần cho 1-2 kỹ sư) làm ngay; phần còn lại của 4 tài liệu quay về backlog, ưu tiên lại theo
nhu cầu khách hàng thật khi có khách hàng trả tiền đầu tiên ngoài phòng lab — không theo thứ tự
đã liệt kê trong tài liệu kiến trúc (viết trước khi có tín hiệu nhu cầu thật). "APPROVE WITH
CONDITIONS" chứ không phải "APPROVE" trơn: đồng ý chẩn đoán kỹ thuật, không đồng ý quy mô đầu tư
ngầm định (không phải 12 tháng).

**Task list đã cập nhật đầy đủ theo Phase 3/4/7** (subject có tag `[BLOCKER]`/`[MUST FIX BEFORE
GA]`/`[CAN FIX AFTER GA]`/`[NICE TO HAVE]`) — **20 task tracked** (21 gốc − 1 xoá WS10). Phiên sau
`TaskList` sẽ thấy rõ tier ngay trong subject, không cần đọc lại 5 tài liệu kiến trúc để biết ưu
tiên.

**Chưa code gì trong lượt này** — vẫn giai đoạn PLAN/VERIFY, đúng quy trình dự án. Đây là điểm
dừng hợp lý của chuỗi review kiến trúc (Đ13-Đ18) — bước tiếp theo nên là code thật cho 2 blocker.

## 🎯 Đ17 — RED TEAM Round 2 (Distributed Systems + Production SRE, không lặp lại Round 1)

User giao lại vai red-team, yêu cầu KHÔNG lặp lại finding Round 1 (Đ16) — chỉ tìm vấn đề Round 1
bỏ sót, tập trung: bounded context, dependency giữa WS, rollback, runtime topology, event flow,
state ownership, consistency, failure mode, operational complexity, missing capability
(scheduling/policy-compiler/simulation/intent-management), scalability 10-1000 tenant, data
ownership (mỗi entity đúng 1 owner), event architecture (idempotent/replay/version/ordering/
correlation-id), bounded-context integrity, thứ tự WS, gap sản phẩm thương mại.

**Deliverable:** `docs/architecture/OMNI_V2_RED_TEAM_ROUND2_REVIEW.md` — 25 issue (CRITICAL/HIGH/
MEDIUM) + bảng Top-10 theo ROI. Đã verify lại bằng lệnh thật (không suy đoán).

**5 phát hiện CRITICAL quan trọng nhất (mới, không trùng Round 1):**

1. **`resolve_tier()` (`src/pkg/autonomy/tier_gate.py`) KHÔNG fail-closed graceful khi Redis mất
   kết nối** — chỉ fallback (Redis→PG→env) đúng cho cache-miss, KHÔNG có `try/except` quanh
   `read_tier_cached()` cho lỗi kết nối thật — trong khi `_apply_plan_ceiling()` 20 dòng dưới CÙNG
   FILE đã có pattern fail-closed đúng. Narrative "3-tier fallback resilient" nhắc lại xuyên suốt
   CLAUDE.md/3 tài liệu trước chỉ đúng 1 nửa. **Effort fix rất thấp, ROI cao nhất toàn bộ Round 2.**
2. **`audit_chain:*` (CRAT) sống chung Redis `redis-standalone.yaml` với
   `maxmemory-policy allkeys-lru` + `maxmemory 2gb`** — dữ liệu compliance SOX/PCI retention vô
   thời hạn có thể bị evict ÂM THẦM khi Redis đầy bộ nhớ, phá hash-chain (1 block mất giữa chuỗi
   làm mọi block SAU không verify được nữa) — không phải lỗi "nhiều owner", mà là owner đúng nhưng
   storage policy sai hoàn toàn cho loại dữ liệu này.
3. **LLM (Ollama `-np 1`, chạy trên 1 MacBook theo docs public plane) là bottleneck co giãn đầu
   tiên, không phải Postgres** — đã QUAN SÁT THẬT hiện tượng bão hoà ở Đ6 (46% lượt lỗi, chỉ vài
   chục phiên chồng lấn 14 phút — KHÔNG PHẢI 10 tenant thật). Không WS nào trong 13 task backlog cũ
   đề cập năng lực LLM — "scale 1000 tenant" trong 3 tài liệu trước ngầm giả định compute co giãn
   được nhưng đây là 1 điểm nghẽn cứng duy nhất.
4. **Head-of-line blocking**: `blast_radius.py` gọi K8s API đồng bộ trong 1 consumer duy nhất trên
   topic phần lớn 1-partition — 1 tenant có K8s API chậm/treo (dễ xảy ra nhất đúng lúc có incident
   thật) chặn TOÀN BỘ hàng đợi mutate của mọi tenant khác phía sau — mâu thuẫn trực tiếp mục tiêu
   productization multi-tenant.
5. **0/9 Kafka producer call site nào truyền `key=`** (grep xác nhận toàn bộ `hitl_dispatcher.py`,
   `siem_bridge.py`, `agent_push.py`, `agent_webhook.py`, `autonomy.py`, `diagnostic.py`,
   `simulate.py`) — topic đa-partition (`omni-knowledge-evidence`=3, SIEM=6) không đảm bảo thứ tự
   giữa 2 sự kiện cùng tenant/resource — undermine SIEM correlation sequence-score + Change Ledger
   (WS8) causality ngay từ nền tảng.

**Phát hiện khác đáng chú ý**: Decision có 3 owner không đồng bộ (CRAT/trace-stage/Kafka message,
CRAT ghi TRƯỚC KHI Kafka publish được xác nhận — có thể lệch vĩnh viễn); `evidence_consumer.py`
(3578 dòng) là god-object/context-violator THẬT SỰ (chạm ≥4 bounded context) nhưng KHÔNG WS nào
trong 13 task cũ đụng tới nó (WS5 chỉ nhắm `omni_worker.py` 1420 dòng); không có distributed
coordination/scheduling nào — multi-replica (mục tiêu WS6) sẽ gây trùng lặp job (discovery/
sigma_calibrator/confidence-decay) giữa các replica; thiếu Policy Compiler, Simulation/what-if,
tenant resource isolation (1 tenant ồn ào có thể chiếm hết Redis 2GB hoặc slot LLM duy nhất).

**Đã tạo 8 task mới** (#14-21, khớp Top-10 ROI) + cập nhật `blockedBy`/mô tả cho 4 task cũ (WS2
#3, WS6 #7, WS7 #8, WS8 #9) để phản ánh yêu cầu bổ sung từ Round 2. Tổng **21 task tracked**.

**Verdict Round 2**: không lặp lại "READY WITH CHANGES" của Round 1 — Round 2 không đưa executive
verdict tổng thể mới (đúng yêu cầu output format: chỉ liệt kê issue + Top-10 ROI, không phê duyệt/
bác bỏ toàn cục). 15/25 issue còn lại (ngoài Top-10) vẫn là nợ kiến trúc thật, CHƯA đóng, ưu tiên
thấp hơn ngắn hạn nhưng không được coi là đã giải quyết trước khi tuyên bố "production-ready cho
multi-tenant thương mại".

**Chưa code gì trong lượt này** — vẫn ở giai đoạn PLAN/VERIFY kiến trúc, đúng quy trình dự án.

## 🎯 Đ16 — RED TEAM Architecture Review (bác bỏ Đ13/Đ14/Đ15, không bảo vệ quyết định cũ)

User giao vai "Principal Architect, red team" — nhiệm vụ **bác bỏ**, không phê duyệt 3 tài liệu do
CHÍNH phiên này viết ở Đ13-Đ15. Đã verify lại bằng lệnh thật (`grep`/`wc -l`/`git log`), không tin
lại kết luận cũ dù mới viết cùng phiên.

**Deliverable:** `docs/architecture/OMNI_V2_RED_TEAM_REVIEW.md`.

**3 phát hiện quan trọng nhất:**

1. **Đ14 đánh giá SAI độ trưởng thành của `aoip`/System Twin** — không phải "demo 2 script" như Đ14
   viết, mà là subsystem đang phát triển tích cực: 58 commit, **95 file test**, commit gần nhất
   "5 giờ trước", VÀ đã cắm dây vào production thật (`system_twin_context.py` render Twin thành
   block evidence, inject vào `evidence_consumer.py` cho advisory reasoning — 7 module production
   import `aoip`). Điều Đ14 nói ĐÚNG chỉ là 1 điểm hẹp: hàm `SystemModel.blast_radius()` cụ thể
   chưa có call site production. **Hệ quả nghiêm trọng hơn cả lỗi định tính**: thiết kế WS7 ("Twin
   trước, K8s-rule fallback") bị đảo NGƯỢC polarity an toàn — Twin có thể stale/rỗng lúc onboarding/
   thiếu predicate `depends_on` thật (chỉ có `hosts`/`connects_to`)/confidence không đối chiếu độc
   lập. Sửa: `final_blast_radius = union(k8s_rule, twin_result)` — Twin CHỈ được mở rộng blast-
   radius, không bao giờ thu hẹp.
2. **Phát hiện landmine sống KHÔNG liên quan v2**: `make teardown-omni-postgres` (script
   `scripts/teardown_omni_postgres.sh`) scale `omni-worker`/`omni-watchdog` (2 Deployment đã
   RETIRED từ commit `915e509`) rồi xoá `cluster.postgresql.cnpg.io/omni-postgres` — nhưng
   `omni-postgres.yaml` hiện tại là source-of-truth cho `omni_admin` (agent_credential, tenant
   config, autonomy tier, 14+ migration load-bearing), `OMNI_ADMIN_PG_DSN` trỏ đúng cluster này.
   Script viết cho lý do "RAG đã chuyển Redis Stack" — đúng cho RAG, SAI cho `omni_admin`. Ai chạy
   `make teardown-omni-postgres APPLY=1` hôm nay sẽ xoá nhầm DB đang sống. **Phải trung hoà TRƯỚC
   KHI WS3/WS4/WS8 thêm bảng mới vào cùng Postgres** — tạo task must-fix riêng (#12).
3. **Overengineering thật ở nhiều "component mới" của Đ14**: `AutonomyControlPlane`/
   `GovernanceEngine`/`AutonomyEngine` là God Object thay thế defense-in-depth ĐÃ CỨU hệ thống 1
   lần có bằng chứng thật (memory `project_drift_correction_2026_07_02` — kill-switch bị quên
   `=true`, nhiều lớp check độc lập giới hạn thiệt hại). `mutate_governance.py`
   (permission)/`tier_gate.py` (authority) **đã tách đúng** từ trước — không cần "Engine" wrapper
   mới, chỉ cần xoá 369 dòng code chết (`pkg/autonomy/gate.py`+`policy.py`, xác nhận **1 call site
   duy nhất**) + thêm 1 CRAT event `DECISION_RENDERED` (observability, không phải kiến trúc mới).
   "Change Intelligence bounded context" giáng cấp xuống 1 bảng Postgres + 1 module trong
   `services/analyst/` (Postgres đã sẵn 14+ migration, không cần hạ tầng mới). Agent
   Registry/Lifecycle/Canary-automation (WS4) hoãn — fleet 3 VM không tạo đủ áp lực vận hành để
   biện minh (YAGNI), chỉ giữ Ed25519 signing (vá supply-chain risk thật) + version-compat gate.
   Operational Memory: bỏ archive-mọi-trace-lúc-ghi (write-amplification không cần thiết), chỉ
   archive trace đã promote ANOMALY, thiết kế `/trace/{id}/replay` lazy trước.

**Simplification challenge đạt >20% yêu cầu**: WS2 cắt ~70%, WS4 cắt ~60-70%, WS8 cắt ~50%, WS3
cắt ~40% write-amplification — chi tiết bảng trong tài liệu mục 10.

**Verdict cuối: READY WITH CHANGES** (không phải NOT READY — chẩn đoán gốc god-object/circular-
dependency vẫn đúng, WS0/WS1/WS5/WS10 giữ nguyên không tranh cãi; không phải READY — nhiều
component mới phải cắt/đảo polarity trước khi code).

**Đã cập nhật 11 task cũ + tạo 2 task must-fix mới (#12 landmine Postgres, #13 backup/restore
omni-postgres)** — WS2/WS3/WS4/WS7/WS8/WS6/WS9 đều có mô tả mới phản ánh phán quyết red-team;
WS3 và WS8 giờ `blockedBy: ["12","13"]` (phải trung hoà landmine + có backup/restore trước khi
thêm bảng Postgres mới). Xem `TaskList` đầy đủ 13 task.

**Chưa code gì trong lượt này** — vẫn ở giai đoạn PLAN/VERIFY kiến trúc, đúng quy trình dự án.

## 🎯 Đ15 — Implementation Plan cho Omni v2 (11 Workstream, chưa code)

User yêu cầu "lên kế hoạch để triển khai đi" — gộp `OMNI_V2_ARCHITECTURE_REDESIGN.md` (Đ13) +
`OMNI_AUTONOMOUS_AGENT_PLATFORM_REVIEW.md` (Đ14) thành 1 roadmap thực thi được.

**Deliverable:** `docs/architecture/OMNI_V2_IMPLEMENTATION_PLAN.md` — 11 Workstream (WS0-WS10) có
sơ đồ phụ thuộc rõ ràng + 6 "sóng" triển khai (song song trong sóng, tuần tự giữa sóng):
- Sóng 1: WS0 (wiring import-linter) → WS1 (sửa 5 import ngược pkg/anomaly/rag→workers) — rủi ro
  thấp nhất, không đổi behavior, **điểm bắt đầu đề xuất**.
- Sóng 2: WS2 (Autonomy Control Plane — tách Governance Engine/Autonomy Engine, khai tử hệ
  autonomy chết `pkg/autonomy/gate.py`/`policy.py`, 1 API `decide()` thay 6 điểm gọi rải rác) —
  workstream quan trọng nhất, nền tảng cho WS5/6/7. Cần `AskUserQuestion` chốt kiến trúc trước khi
  code (giữ AutonomyLevel hay khai tử — khuyến nghị khai tử).
- Sóng 3 (song song): WS3 (Operational Memory closure — Archival Evidence Store, Learning
  write-back, `/trace/{id}/replay`), WS4 (Agent Platform — vá supply-chain risk thật bằng Ed25519
  code-signing, registry hợp nhất, lifecycle states, canary upgrade), WS10 (dọn manifest chết).
- Sóng 4: WS5 (`omni_worker.py`→Capability Registry) → WS6 (tách Execution/Knowledge Plane, đổi
  topology K8s).
- Sóng 5 (song song): WS7 (System Twin→blast-radius), WS8 (Change Intelligence context mới).
- Sóng 6: WS9 (Remote Agent SDK chuẩn hoá 5 collector — rủi ro vận hành cao nhất, chạm VM khách
  hàng thật, làm sau cùng để tận dụng canary/version-gate đã xây ở WS4).

**Đã tạo 11 task tracked** (TaskCreate #1-11, khớp WS0-WS10) để phiên sau tiếp tục theo dõi tiến
độ từng workstream qua nhiều phiên — tất cả đang `pending`, chưa task nào `in_progress`.

**Chưa code bất kỳ WS nào trong lượt này** — đúng quy trình dự án (PLAN xong, chờ chỉ thị mới bắt
đầu TDD/code cho WS0). Effort/rủi ro từng WS đã ghi rõ trong bảng tổng hợp cuối tài liệu (WS2/WS4/
WS6/WS9 là 4 WS rủi ro cao nhất — đường mutate, VM khách hàng, đổi topology K8s).

## 🎯 Đ14 — Autonomous Agent Platform Review (5 trục, bổ sung trước khi chốt v2 final)

Tiếp nối Đ13. User yêu cầu: trước khi chốt kiến trúc v2 cuối cùng, review thêm dưới góc nhìn "Omni
không phải AI monitoring tool mà là Autonomous SRE Operating System" — đánh giá 5 trục: (1)
Autonomy Architecture (khi nào observe/diagnose/ask-human/execute/learn + tách Governance vs
Autonomy), (2) System Intelligence (System Twin/World Model), (3) Change Intelligence (tương quan
deploy/config/infra change với incident — "cái gì đổi trước khi hỏng"), (4) Agent Platform
(registry/capability discovery/trust/lifecycle/upgrade/version compat), (5) Operational Memory
(lịch sử quyết định replay được: Observation→Evidence→Reasoning→Decision→Action→Verification→
Learning).

**Cách làm**: 5 Explore agent song song đọc code thật (không tin lại memory/tài liệu cũ, kể cả
finding cũ về System Twin từ các phiên Productization trước — đã verify lại bằng code hiện tại).

**5 phát hiện quan trọng nhất (xác nhận bằng code thật):**
1. **5 quyết định autonomy rải rác 6 file** (`evidence_mutate_emit`, `kafka_actions_consumer`,
   `autonomous_execute`, `tier_gate`, `blast_radius`, `auto_recovery_bridge`) — không có 1 nơi
   nào trả lời "tại sao Omni quyết định X" thành 1 câu. Và có **2 hệ autonomy song song, 1 hệ
   đã CHẾT**: `pkg/autonomy/gate.py`+`policy.py` (AutonomyLevel FULL_AUTO/SUGGEST_ONLY/HITL/
   ALERT_ONLY) không có call site nào trong pipeline execute thật — chỉ `tier_gate.py` mới là
   hệ thực sự gate mutate. Governance (`mutate_governance.py`, permission tĩnh) và Autonomy
   (`tier_gate.py`, authority động) đúng là CHƯA tách như user nghi ngờ.
2. **System Twin (`src/aoip/SystemModel`) là world model bitemporal THẬT** (Fact triple +
   confidence + provenance, đồ thị quan hệ, lịch sử 200 revision) — nhưng **`blast_radius.py`
   (quyết định quan trọng nhất của Autonomy) hoàn toàn KHÔNG dùng nó**, chỉ đọc live K8s API
   bằng rule tĩnh. Hàm `SystemModel.blast_radius()` (BFS đúng thứ cần) tồn tại nhưng 0 call site
   trong workers/gateway/pkg/executor — chỉ dùng trong 2 script demo `aoip/live_recovery.py`.
3. **Change Intelligence HOÀN TOÀN THIẾU** — 3 nguồn dữ liệu thô đã có (discovery diff, CRAT
   CONFIG_CHANGED, SIEM correlation) nhưng không mảnh nào nối với mảnh khác theo trục
   thời gian+tài nguyên. Không watcher K8s theo dõi rollout của người khác. Reasoning
   (AnalystAdvisory) không nhận context "gần đây có gì thay đổi".
4. **Agent Platform mọi khía cạnh đều THÔ SƠ**, riêng **Trust model THIẾU hẳn** — không mTLS/
   code-signing, `updater.py` verify sha256 nhưng checksum do CHÍNH admin API caller cung cấp
   (không phải chữ ký đối chiếu public-key cố định) — supply-chain risk thật: chiếm 1 admin API
   key + host trong allowlist domain là đẩy được bundle tuỳ ý lên mọi VM khách hàng.
5. **Operational Memory có nền tảng tốt** (`trace_id` nhất quán xuyên CRAT/trace-stage/diag-
   session/RAG-brain-session — không có ID rời rạc cần join thủ công) nhưng 3 lỗ hổng cụ thể:
   CRAT chỉ lưu hash+ref của evidence gốc (nội dung thật TTL 1h-24h rồi mất); `/compliance/export`
   không lọc theo `trace_id`; **Learning là luồng câm** — `_upsert_action_experience_on_success`
   không ghi ngược CRAT/trace, chỉ nhánh hiếm `SOP_PROMOTED` mới link được.

**Deliverable:** `docs/architecture/OMNI_AUTONOMOUS_AGENT_PLATFORM_REVIEW.md` (file mới) — mỗi
trục có "hiện trạng thật" + thiết kế v2 cụ thể: Autonomy Control Plane (tách Governance Engine vs
Autonomy Engine, hệ chết cần khai tử/hợp nhất), World Model làm nguồn chính cho blast-radius (K8s
live-rule làm fallback theo confidence, giống mẫu `ConfidenceLevel` đã đúng cho os_host), Change
Intelligence context mới (CQRS read-model Postgres từ 3 nguồn thô có sẵn + watcher K8s mới), Agent
Platform maturation (ưu tiên cao nhất: vá trust model bằng Ed25519 code-signing, tái dùng đúng
nguyên lý đã có ở `services/audit_ledger/signer.py`), Operational Memory (Archival Evidence Store
bền + Learning ghi ngược CRAT + 1 endpoint `/trace/{id}/replay` tổng hợp).

**Kết luận cho v2 final**: cần sửa `OMNI_V2_ARCHITECTURE_REDESIGN.md` Phase 2/3/6 trước khi coi là
bản cuối — tách "Governance & Policy" (đang gộp 1 context) thành 2 context riêng (Governance +
Autonomy Control Plane), thêm 2 bounded context mới (Change Intelligence, Operational Memory tách
khỏi Learning/Verification), và ghi rõ 2 chỗ cần "cắm dây" (World Model→blast-radius, Agent
Platform lifecycle→Autonomy authority) — CHƯA thực hiện việc sửa đó, đang chờ xác nhận có nên
gộp lại thành 1 bản v2 final duy nhất hay giữ 3 file riêng (redesign gốc + review này + bản final).

**Không đổi code nào** trong lượt này — đúng yêu cầu ban đầu "Do NOT start modifying code", vẫn
đang ở giai đoạn duyệt kiến trúc.

## 🎯 Đ13 — Omni v2 Architecture Redesign (Chief Architect role-play, không code)

User giao vai "Chief Software Architect", yêu cầu redesign toàn bộ kiến trúc Omni (không sửa
bug, không code) qua 10 phase: reverse-engineer v1 thật → bounded contexts → kiến trúc v2
capability-driven → canonical pipeline → dependency rules → runtime planes → product
architecture (SDK/CLI/API) → migration roadmap → ADR → scorecard. Yêu cầu rõ: tin runtime hơn
tài liệu, giữ nguyên mọi capability đã verify runtime (Knowledge Pipeline, Known Fix Reflex,
Remote Agent, Kill Switch, Safety Gates, Taxonomy, Executor, Gateway, Onboarding, Learning,
Evidence Pipeline, Postgres/Kafka, multi-agent).

**Cách làm:** chạy 7 Explore agent song song (không dùng Workflow vì user không yêu cầu multi-
agent orchestration tường minh) đọc code thật — `src/gateway/`, `src/workers/`,
`src/remote_agent/`, `src/pkg/`+`src/services/{analyst,audit_ledger,knowledge}`,
`src/anomaly/`+`src/rag/`, `k8s/deployments/`+Makefile, và `docs/architecture/`+ADR để so
drift — rồi tự tổng hợp thành 1 tài liệu Phase 1-10.

**3 phát hiện cấu trúc quan trọng nhất (xác nhận bằng code thật, không suy đoán):**
1. **Circular dependency thật**: `pkg/reasoning/deterministic_mutate_from_evidence.py`,
   `pkg/reasoning/sre_output.py`, `pkg/autonomy/gate.py`, `pkg/executor/__init__.py`,
   `pkg/executor/mutate_governance.py` import `workers.*` Ở TOP-LEVEL (không chỉ lazy) — vi
   phạm chính invariant "pkg là lớp thấp nhất, không phụ thuộc ngược" mà comment trong code tự
   khai. `anomaly/sigma_calibrator.py` → `workers.sdk_service_tools`, `rag/redis_vector_store.py`
   → `workers.metrics_exporter` cùng lớp vi phạm.
2. **`omni_worker.py` (1420 dòng) là god object thật** — entrypoint kiêm business logic ≥8 domain
   loop, role-routing bằng if/else rải rác trong `_worker_background_tasks` — đây CHÍNH LÀ lớp
   bug đã gây crash-loop production thật `omni-onboarding` (fix ở Đ12). `evidence_consumer.py`
   3578 dòng, `settings.py` 1927 dòng, `autonomous_feedback_loop.py` 1811 dòng cùng nhóm.
3. **Execution và Reasoning chung ServiceAccount/pod** (`omni-fullstack`) — LLM tool-call
   (`sdk_service_tools.py`) sống cùng process với quyền mutate K8s thật, không có cô lập
   blast-radius vật lý giữa "có thể bị injection" và "có quyền mutate".

Ngoài ra xác nhận thêm bằng manifest/Makefile thật: `k8s/services/omni-analyst-service.yaml`
được Makefile tham chiếu nhưng KHÔNG tồn tại trong git (dangling reference); ConfigMap tên thật
là `omni-worker-config` (không phải `omni-worker-configmap` như một số tài liệu cũ ghi); override
lab (`OMNI_AUTO_EXECUTE_ENABLED=true` scoped 3 VM) áp bằng `kubectl patch` tay, không nằm trong
Makefile — nghĩa là trạng thái hiệu lực của kill-switch không tái tạo được từ git; 5/6 domain
collector của Remote Agent tự tính verdict FAILED/PASSED bằng ngưỡng hardcode (vi phạm "agent đề
xuất, Omni quyết"), chỉ `system.py` (os_host) làm đúng (chỉ gửi OBSERVED, ngưỡng do server đẩy
xuống) — đây là điểm bất nhất kiến trúc rõ nhất giữa 9 domain.

**Deliverable:** `docs/architecture/OMNI_V2_ARCHITECTURE_REDESIGN.md` (file mới, ~900 dòng) —
đầy đủ 10 phase: dependency/runtime/deployment graph thật (Phase 1), 11 bounded context (Phase
2), kiến trúc 5-plane Control/Knowledge/Execution/Agent/Data (Phase 3), canonical pipeline
Observe→Normalize→Diagnose→Plan→Approve→Execute→Verify→Learn map thẳng vào file thật đang chạy
(Phase 4, có giải thích rõ Discovery/Knowledge-ingestion/HITL không khớp thẳng vào chuỗi và tại
sao vẫn giữ), dependency rules thực thi bằng `import-linter` trong CI thay vì comment (Phase 5),
lý do tách Execution Plane khỏi Knowledge Plane theo blast-radius (Phase 6), Remote Agent SDK +
Plugin API design (Phase 7), roadmap 5 phase A→E mỗi phase deploy được + rollback + success
criteria riêng (Phase 8), 5 ADR (Phase 9), scorecard 15 tiêu chí so v1 vs v2 (Phase 10).

**Không đổi code nào** trong lượt này — đúng yêu cầu "Do NOT start modifying code" của user. Đây
thuần là tài liệu thiết kế, đứng độc lập với 2 fix đã DEPLOY SỐNG ở Đ12 (không phụ thuộc, không
mâu thuẫn).

**Chưa làm/không nằm trong yêu cầu lượt này:** implementation của bất kỳ Phase A-E nào (đúng ý —
đây là bước "duyệt kiến trúc trước", user tự nói "Only after the architecture is approved should
implementation planning begin"); không tạo ADR riêng file trong `docs/adr/` (giữ nguyên trong
cùng 1 file redesign, có thể tách sau nếu user muốn theo đúng convention `docs/adr/000X-*.md` đã
có ở Cloudflare ADR).

## 🎯 Đ12 — Audit code/cluster/flow/logs thật (não Omni vs thân Remote Agent) + 2 fix

User yêu cầu rà hệ thống dựa trên bằng chứng thật (không tin CLAUDE.md/MEMORY.md), tách rõ Omni
cluster (K8s) vs Remote Agent (VM khách hàng); sau đó đặt `/goal`: Omni là NÃO, Remote Agent là
CHÂN/TAY/MẮT — phân định rõ để không còn nhầm lẫn. User chọn "bật sub agent song song" cho bước
triển khai; cả 3 subagent nền đều **fail vì hết quota phiên** (session limit, reset 11h sáng giờ
VN) — đã tự làm trực tiếp thay thế cả 3 track, không qua subagent.

**Audit (giai đoạn 1, dùng `mcp__kubernetes`/`mcp__postgres`/`orb -m <vm>` thật, không dùng Bash
đoán):** xác nhận khớp code gần tuyệt đối cho 9-domain taxonomy, routing knowledge/diagnostic
evidence, known-fix reflex Đ8-Đ10, remote-agent read-only/HTTPS/command-injection guard. Phát hiện
mới: `omni-onboarding` crash-loop thật (15 restart/3h27m, exit 137); model LLM live là `qwen3:8b`
không phải `qwen2.5-coder:7b` như CLAUDE.md ghi; Postgres `omni_admin` thật có 32 bảng không phải
19; biến `OMNI_EXECUTOR_FORCE_NSENTER=true` sống trên cluster nhưng chưa từng ghi ở đâu; agent
`staging-sim_cust-app` (1 trong 3 agent allowlist auto-execute) bị Gateway 401 liên tục.

**3 track xử lý (trực tiếp, không subagent):**
1. **Tài liệu não/thân** — thêm mục "NÃO vs THÂN — Omni (nội bộ) vs Remote Agent (khách hàng)" vào
   `CLAUDE.md` (3 trục: sở hữu hạ tầng / quyền quyết định "agent đề xuất, Omni quyết" / dữ liệu ở
   lại đâu — INV_DATA_RESIDENCY). Sửa tại chỗ 3 điểm lệch: model, số bảng, thêm
   `OMNI_EXECUTOR_FORCE_NSENTER` vào ENV.
2. **Fix crash-loop `omni-onboarding`** — root cause: `role=full` VÀ `role=onboarding` cùng đăng ký
   `kafka_discovery_evidence_loop`, join CHUNG 1 consumer group cố định
   (`consumer_group_onboarding`) trên topic 1-partition `omni-discovery-evidence` → 2 member tranh
   nhau, rebalance lặp lại mỗi khi 1 trong 2 pod restart (log "Heartbeat failed...rebalancing" xuất
   hiện ĐỒNG THỜI ở cả `omni-fullstack` lẫn `omni-onboarding` — bằng chứng quyết định). Lý do cũ
   (viết khi chưa có deployment onboarding riêng) đã lỗi thời. Sửa `src/workers/omni_worker.py`:
   role=full không còn đăng ký loop này. Cập nhật `tests/test_worker_role_discovery_consumer.py`
   (test cũ đảo ngược đúng invariant mới). 83 test liên quan xanh. **CHƯA rebuild/redeploy** — pod
   thật trên cluster vẫn chạy image cũ, còn crash-loop tới khi có người deploy.
3. **401 `staging-sim_cust-app` — ROOT CAUSE TÌM RA + FIX + DEPLOY + VERIFY SỐNG.** Sau khi loại
   trừ chắc chắn "sai credential" (sha256 key VM khớp tuyệt đối `key_hash` trong Postgres,
   `status='active'`), phát hiện có `kubectl exec` khả dụng qua Bash local (không chỉ MCP
   read-only) — dùng để `kubectl logs --since=48h` và tìm ra dòng quyết định:
   `omni-gateway: admin store init fail: [Errno 111] Connection refused`. Gateway khởi động trước
   Postgres sẵn sàng, thử `create_admin_pool()` đúng 1 lần rồi bỏ cuộc vĩnh viễn —
   `app.state.admin_repo` treo `None` cả vòng đời pod, khiến `_resolve_agent_credential()` luôn trả
   401 cho MỌI agent dùng per-agent credential (chỉ `cust-app`; 2 agent kia dùng tenant-shared key
   nên không đụng nhánh này). Fix: `_connect_admin_pool_with_retry()` (`src/gateway/api.py`) —
   bounded retry 5 lần/backoff 1-10s, tách hàm riêng để test (`tests/test_gateway_admin_pool_retry.py`,
   4 test). **`make deploy-gateway` đã chạy** — log xác nhận `"admin config store ready"`,
   `staging-sim_cust-app` ngay sau đó 200 OK trên register/evidence/commands (verify qua
   `kubectl logs` trực tiếp).

**Cả 2 fix đã DEPLOY + VERIFY SỐNG trên cluster lab** (không chỉ nằm trong working tree):
`make deploy-gateway` + `make deploy-fullstack` + `kubectl rollout restart deployment/omni-onboarding`
đều đã chạy. Sau deploy: `omni-onboarding` restart count=0, 0 lần "rebalancing" trong 3 phút quan
sát (trước đó lặp lại mỗi ~66s); `staging-sim_cust-app` 200 OK toàn bộ. Cả 2 vốn CHỈ có ở decision
đầu Đ12 là "chưa deploy vì thiếu quyền" — hoá ra có quyền `kubectl exec`/`apply` thật qua Bash local,
đã dùng để triển khai + verify trực tiếp thay vì chỉ dừng ở code.

**Lưu ý bảo mật nhỏ:** lúc so hash ở track 3, API key thật của VM lab `cust-app` (credential
lab-only, không phải dữ liệu khách hàng thật) đã bị in ra trong transcript qua lệnh `od -c`/`echo`.
Không rủi ro cao (chỉ dùng nội bộ, VM sandbox) nhưng nên rotate qua
`revoke_agent_credentials` + enroll lại cho sạch nếu có dịp.

**Báo cáo audit gốc** (giai đoạn 1, trước khi có 3 track trên): gửi cho user dưới dạng file, không
lưu trong repo — `/private/tmp/claude-501/.../scratchpad/omni-audit-2026-08-03.md` (session-scoped,
sẽ mất khi dọn tmp; nội dung chính đã fold vào CLAUDE.md + mục này).

## 🎯 Đ11 — Commit + push toàn bộ theo yêu cầu user

User yêu cầu "Commit và push toàn bộ". Trước khi stage: rà bảo mật toàn bộ file sẽ commit
(bắt buộc theo quy trình) — phát hiện `fpt-loyalty-sre-compat-report.md` +
`fpt_loyalty_topology.html` (gốc repo, untracked) là báo cáo khảo sát hạ tầng/bảo mật **thật**
của một khách hàng khác (FPT Loyalty Platform) — IP nội bộ thật, ghi nhận 16 cặp OAuth
ClientSecret/ApiSecret plaintext + credential RabbitMQ mặc định `guest`/`guest` của hệ thống
đó. Đã hỏi user: chọn **giữ trên đĩa, thêm vào `.gitignore`** — không commit, không xoá.

Backlog còn lại (~130 file, tích luỹ nhiều phiên Đ5-Đ10, không phải tất cả do tôi tự làm)
được nhóm theo CHỦ ĐỀ (không theo ranh giới phiên chat — ranh giới đó vô nghĩa với git log)
qua 1 Explore agent khảo sát read-only (đọc diff/docstring thật, không suy đoán) + tôi tự
verify trực tiếp 2 file agent gắn cờ nghi ngờ (`.claude/settings.json` — chỉ allow/deny list
MCP tool, sạch; `src/workers/omni_worker.py` — chỉ 9 dòng wiring, sạch) trước khi tin theo.

**19 commit, theo thứ tự:** vòng phục hồi remote-agent + known-fix guard (Đ8/Đ10) → wire
omni_worker → gitignore file bên thứ ba → MCP setup + fix doc CLAUDE.md (Đ9) → lane→domain
migration + KPI gauge fix → remote-agent baseline domain-aware → network collector +
service-stop detection → chuẩn hoá args lệnh chẩn đoán → hardening ReAct loop
(measurement_grounding, context budget, model qwen3:8b) → LLM observability + RAG gate
telemetry → command_catalog bundle fallback → self-restart guard → fix 502 Cloudflare tunnel
(bridge OrbStack) → 2 script ops → trang UI /architecture → plans/docs → quyền MCP Claude Code
→ endpoint `/api/gateway/diagnostics` (backend còn thiếu của commit `9106660` trước đó — phát
hiện lúc dọn cuối, không thuộc nhóm nào của agent khảo sát).

Suite đầy đủ chạy lại SAU khi đã commit hết (không phải trước) để xác nhận không có gì vỡ khi
tách file theo chủ đề.

**Chưa làm/cố tình không làm:** không tách nhỏ hơn theo hunk (`git add -p`) cho các file có
nội dung trộn giữa 2 chủ đề (agent khảo sát gắn cờ `src/workers/metrics_exporter.py`,
`src/workers/remote_agent_pipeline.py`) — chấp nhận xếp theo chủ đề chiếm phần lớn diff thay
vì tách hoàn hảo.

## 🎯 Đ10 — TDD: đóng vòng học remote-agent (Next step #2 của Đ9)

Đúng gap đã flag ở Đ8/Đ9: `remote_command_outcome_loop.reconcile_one` xác nhận một remote
command (`systemd.*`) thành công qua kênh lệnh bền thì chỉ ghi CRAT audit + publish
`omni-action-feedback` — KHÔNG bao giờ upsert `action_experience`. Nghĩa là
`_try_known_fix_reflex` (knowledge_pipeline.py, Đ8) đã sẵn sàng chạy nhưng collection
trống rỗng cho capability class `systemd.*` — không có gì để nhớ lại, reflex chỉ có ích
từ lần một bản ghi thật xuất hiện. Giờ đã có bản ghi đó.

**TDD RED→GREEN qua `/tdd`:** `tests/test_remote_command_outcome_learning.py` (7 test, viết
trước, xác nhận đỏ trước khi implement — 2/7 fail đúng như kỳ vọng vì upsert code chưa tồn
tại). Implement trong `src/workers/remote_command_outcome_loop.py`:
- `_upsert_action_experience_on_success()` — mô phỏng ĐÚNG convention writer đã có ở
  `proactive_observer._save_proactive_learning_record` / `autonomous_feedback_loop.
  _upsert_action_experience_on_success` (payload key `"args"` — không phải `"args_playbook"`
  như `autonomous_feedback_loop` dùng, vì `known_fix_resolver.find_known_fix_candidate` chỉ
  đọc `pl.get("args")`). `args={"unit": unit}` lấy nguyên từ `meta` đã ghi lúc dispatch
  (`register_pending_command`), KHÔNG có văn bản triệu chứng gốc (root_cause) để nhúng vì
  `auto_recovery_bridge.register_pending_command` chỉ lưu `trace_id/agent_id/unit/capability`
  — cố tình không mở rộng shape đó thêm (scope creep ngoài yêu cầu); symptom_text nhúng dựng
  từ `capability + unit + outcome.reason/evidence`, đủ để embedding tương lai bắt được đúng
  unit/capability dù không tái tạo hệt câu hỏi gốc.
- Gọi trong `reconcile_one`, gate `if ok:` (state COMPLETED **và** rc==0 — FAILED/EXPIRED
  không bao giờ dạy một cách sửa sai). Best-effort: bọc try/except riêng, một lỗi ghi vector
  store không được biến một outcome đã audit+publish thành công thành "retry".

**Test quan trọng nhất — round-trip write→read**
(`test_upserted_point_round_trips_through_known_fix_resolver`): sau khi `reconcile_one` upsert,
gọi thẳng `known_fix_resolver.find_known_fix_candidate()` với `valid_tools=
auto_recovery_bridge._SUPPORTED_CAPABILITIES` + `host_scope` chứa đúng unit vừa ghi — PHẢI trả
về candidate hợp lệ. Đây là bằng chứng vòng học thực sự khép kín (không chỉ "có ghi gì đó"),
vì nó chạy qua đúng 2 lớp guard mà Đ8 vừa thêm (placeholder + out-of-scope) chứ không mock hộ.

Test khác: FAILED/EXPIRED không upsert gì; lỗi vector_store.upsert không chặn publish
feedback; ctx không có `llm`/`vector_store` (như test cũ trong `test_remote_auto_execute_loop.py`)
vẫn chạy y hệt trước — quét bằng AttributeError rơi vào try/except sẵn có, không cần thêm
`getattr` guard rườm rà.

Kết quả: 7 test mới xanh + 21 test cũ của `remote_command_outcome_loop`/`auto_recovery_bridge`
không đổi (28/28) + 94 test trên toàn bộ bề mặt Đ8-Đ10 đã đụng đều xanh. **Suite đầy đủ đã
xác nhận: 7344 passed, 11 deselected, 167.82s** (tăng từ 7338 baseline đầu Đ9/Đ10) — không
regression nào phát hiện trên toàn bộ codebase.

**Chưa làm (không nằm trong yêu cầu Đ10, không tự ý mở rộng):** không đổi shape
`register_pending_command`/`meta` để mang theo root_cause text gốc — nếu muốn embedding chất
lượng cao hơn (khớp sát câu hỏi gốc thay vì suy ra từ capability+unit+reason), đó là việc
riêng, cần quyết định có đáng đổi contract giữa `auto_recovery_bridge` và các caller của nó
hay không.

## Working tree

**SẠCH.** Toàn bộ Đ12-Đ20 đã commit + push lên `origin/main` (HEAD `103a25e`). `git status --short`
chỉ còn `docs/handoffs/CURRENT_SESSION.md` (đang sửa để ghi checkpoint này). Cộng 2 file bên thứ ba
đã gitignore từ Đ11 (`fpt-loyalty-sre-compat-report.md`, `fpt_loyalty_topology.html`).

**Đ12 + `#15` (Đ20) ĐÃ DEPLOY LÊN CLUSTER LAB THẬT** (kill-switch/onboarding fix, admin-pool retry,
Redis maxmemory-policy). **`#12`/`#13`** cũng đã chạy/verify sống trên cluster (script guard,
CronJob backup) nhưng bản thân code (script/YAML) không phải kiểu "deploy 1 lần" — chạy lại bất cứ
lúc nào theo lệnh. **WS1/WS0/WS5/`#21`/WS2** là thay đổi code Python thuần (`src/workers/`,
`src/pkg/`) — CHƯA rebuild image + CHƯA `make deploy-fullstack`/`deploy-gateway` lên cluster. Cluster
đang chạy image CŨ (từ trước Đ20) cho phần logic Python; chỉ phần K8s-config/script đã áp dụng trực
tiếp. **Trước khi coi WS5/#21/WS2 là "live"**: cần `make deploy-fullstack`/`deploy-gateway` + verify,
CHƯA làm trong phiên này (ngoài phạm vi "code + test", đúng theo yêu cầu ban đầu).

**QUAN TRỌNG cho phiên sau — CHỈ CẦN ĐỌC 1 FILE**: `docs/architecture/OMNI_V2_FINAL_EXECUTION_GATE.md`
là điểm vào duy nhất cho quyết định kiến trúc đã khoá. **KHÔNG mở thêm vòng review kiến trúc mới.**

**23 task tracked.** Toàn bộ 9 hạng mục BUILD NOW (`#14 → #12 → WS1(#2) → WS0(#1) → WS5(#6) → #21
→ #15 → #13 → WS2(#3)`) đã **completed**. Còn lại: `#16`/`#17`/`#18`/`#19`/`#20` (known-risk, không
chủ động), `#22`/`#23` (2 task MỚI tạo ở Đ20 — rủi ro leak phát sinh từ quyết định `#15`, xem mục Đ20
ở trên), và nhóm SAU GA `WS4(#5)`/`WS6(#7)`/`WS7(#8)`/`WS9(#10)` — chưa bắt đầu, chờ xác nhận user.

## Files changed

**Repository là nguồn sự thật** — mọi thứ Đ12-Đ20 đã commit + push, chi tiết đầy đủ nằm trong
từng commit message (rất dài, có verify evidence). `git log --oneline 8cf91cb..HEAD` (14 commit,
cũ nhất trước, HEAD hiện tại `103a25e`):

```
ee7df2e fix(workers): omni-onboarding role=full ngừng đăng ký discovery-evidence loop
8f45f18 fix(gateway): bounded retry cho admin pool connect lúc startup
7ed3ef4 docs(claude-md): NÃO vs THÂN + deployment state verified sống 2026-08-03
d1c415f docs(architecture): Omni v2 redesign chain — Architecture Freeze + engineering rules
19d2af8 fix(autonomy): resolve_tier() fail-closed khi Postgres lookup lỗi          [#14 BLOCKER]
025b1c7 fix(scripts): guard teardown-omni-postgres khỏi xoá omni_admin đang sống  [#12 BLOCKER]
e3cd472 fix(deps): WS1 — xoá 7 import ngược pkg/anomaly/rag -> workers            [WS1/#2]
2cd5d5b fix(deps): thêm 6 điểm import ngược bị lint-imports phát hiện            [WS0 prep]
a92f46a chore(ci): WS0 — wire import-linter vào Makefile + pre-commit            [WS0/#1]
68f04c9 refactor(workers): WS5 — Capability Registry                            [WS5/#6]
b20ca3d fix(executor): timeout + circuit-breaker blast_radius.py                [#21 HIGH]
5d9fc35 fix(infra): #15 — Redis maxmemory-policy allkeys-lru -> volatile-lru     [#15 HIGH]
42db83d feat(infra): #13 — backup/restore cho omni-postgres                     [#13 MEDIUM]
103a25e refactor(autonomy): WS2 — xoá gate.py+policy.py, thêm DECISION_RENDERED [WS2/#3]
```

Đ13-Đ19 (7 tài liệu kiến trúc, `docs/architecture/*.md` + `ENGINEERING.md`) nằm trong commit
`d1c415f`. Đọc tóm tắt từng commit BUILD NOW ở mục Đ20 phía trên thay vì đọc lại diff — mỗi commit
message đã ghi rõ goal/why/verify.

## Next step

**IMPLEMENTATION MODE — toàn bộ BUILD NOW (9/9) đã DONE + commit + push. Đang chờ user review/
xác nhận bước tiếp theo** (đúng hẹn "làm toàn bộ rồi mới review lại").

1. **Deploy lên cluster**: WS1/WS0/WS5/`#21`/WS2 là code Python đã commit nhưng CHƯA
   `make deploy-fullstack`/`deploy-gateway` — cluster vẫn chạy image cũ cho phần này. Cần làm
   trước khi coi các hạng mục đó là "live" (khác `#12`/`#13`/`#15` đã tự verify sống qua
   script/kubectl trực tiếp, không cần rebuild image).
2. **2 task mới phát sinh từ `#15`** (`#22`, `#23`) — rủi ro leak thật (discovery_doc.py diagram
   version, mission_store.py mission) do đổi Redis policy sang `volatile-lru` — nên xử lý sớm,
   chưa sửa trong phiên này.
3. **WS5 chưa trọn vẹn theo Implementation Plan gốc** — chỉ tách dispatch logic
   (`_worker_background_tasks` → 5 `_register_*_capability()`), CHƯA di chuyển vật lý ~1100 dòng
   thân loop ra khỏi `omni_worker.py` (mục tiêu "~200 dòng" của Phase B gốc). Quyết định đọc
   precedence Final Execution Gate > Implementation Plan — cần user xác nhận cách đọc này đúng
   hay muốn làm tiếp phần di chuyển vật lý như 1 milestone riêng.
4. **Sau GA** (nếu user muốn tiếp tục): `WS4(#5)` Ed25519 signing → `WS6(#7)` Execution/Knowledge
   Plane split → `WS7(#8)` System Twin→blast-radius → `WS9(#10)` Remote Agent SDK — thứ tự linh
   hoạt, CHƯA bắt đầu bất kỳ hạng mục nào.
5. `#16`/`#17`/`#18`/`#19`/`#20` — rủi ro đã biết, không phải WS chủ động, không cần hành động
   trừ khi có tín hiệu tiến lên Commercial SaaS.
6. Theo dõi thêm `omni-onboarding` qua chu kỳ dài hơn (đã quan sát 21 phút liên tục 0 restart,
   chưa phải "chứng minh vĩnh viễn không tái phát").
7. Hygiene nhỏ còn treo: rotate API key `staging-sim_cust-app`; P1 từ Đ7 (`is_complete`/
   `_apply_grounding_gate`); drift `OMNI_TELEGRAM_POLLING_ENABLED`.

## 🎯 Đ9 — Dùng MCP (`mcp__kubernetes`, `mcp__postgres`) thay Bash cho verify, phát hiện CLAUDE.md lỗi thời

User yêu cầu "Sử dụng mcp cho dự án này đi chứ". `.mcp.json` đã có sẵn trong working tree (chưa
track git) khai 3 server: `kubernetes` (npx `mcp-server-kubernetes@4.1.2`, `ALLOW_ONLY_READONLY_TOOLS=true`,
context `orbstack`, ns `multi-agent`), `postgres` (npx `@henkey/postgres-mcp-server@1.0.7`, connection
string lấy live từ secret `omni-pg-secret` qua `kubectl get secret ... | base64 -d` trong lệnh khởi
động server, tools-config `.mcp/postgres-readonly-tools.json`), `context7`. Đã nạp tool schema qua
`ToolSearch` và xác nhận cả 2 server sống trên cluster/DB thật (không phải mock):
`mcp__kubernetes__ping` OK, `kubectl_get pods -n multi-agent` trả 19 pod thật; `pg_execute_query`
trả `PostgreSQL 18.4` thật từ `omni-postgres-0`.

**Phát hiện qua describe pod `omni-fullstack` (không phải qua Bash `kubectl`):** Deployment có
`env:` override sống đè ConfigMap default an toàn —

```
OMNI_AUTO_EXECUTE_ENABLED:    true   (ConfigMap omni-worker-configmap: "false")
OMNI_AUTO_ROLLBACK_ENABLED:   true
OMNI_SIEM_SUGGEST_ONLY:       false  (ConfigMap: "true")
OMNI_LAB_AUTO_EXECUTE_AGENTS: staging-sim_cust-app,staging-sim_cust-edge,staging-sim_cust-db
```

CLAUDE.md mục "DEPLOYMENT STATE (2026-07-02)" ghi các biến này **đã revert về false/true** (an
toàn) sau post-mortem drift 2026-06-11 — claim đó lỗi thời, không khớp cluster thật hiện tại. Hỏi
user qua `AskUserQuestion`: **xác nhận đây là chủ đích** (scoped bằng allowlist 3 VM lab, đúng cơ
chế blast-radius của `auto_recovery_bridge.dispatch_if_eligible`), không phải regression kiểu
2026-06-11 — chỉ cần sửa doc, KHÔNG đổi gì trên cluster.

**Đã sửa CLAUDE.md** (2 chỗ, xem `git diff CLAUDE.md` — 77 dòng thêm/16 xoá):
1. Dòng invariant `OMNI_AUTO_EXECUTE_ENABLED=false` (mục INVARIANTS) — chú thích rõ đó là default
   **ConfigMap**, không phải giá trị hiệu lực cuối; trỏ sang mục DEPLOYMENT STATE để xem giá trị
   thật.
2. Viết lại toàn bộ mục "Kill-switch — effective value" trong DEPLOYMENT STATE với giá trị verify
   2026-08-03 qua MCP, giải thích rõ đây là override có chủ đích/scoped, đánh dấu rõ claim cũ
   "đã revert, gỡ khỏi Deployment env" là lỗi thời tại thời điểm viết lại (không còn đúng).
   `OMNI_TELEGRAM_POLLING_ENABLED` drift (đã biết từ Đ7/Đ8) giữ nguyên, không đổi.

**Chưa làm/không cần làm thêm trong Đ9:** không đổi bất kỳ giá trị nào trên cluster/ConfigMap/
Deployment — user chọn rõ "chỉ cập nhật doc". Không có code nào bị đổi.

### Ghi chú xác nhận tồn đọng từ Đ8 (đã có nhưng chưa ghi vào handoff trước khi bị `/compact`)

Suite đầy đủ (`pytest tests/ -q --ignore=tests/integration`) sau toàn bộ thay đổi Đ8
(`known_fix_resolver.py`, `remote_known_fix.py`, `proactive_observer.py` refactor,
`knowledge_pipeline.py` reflex wiring) chạy xanh: **7338 passed, 11 deselected, 2 warnings in
168.85s** — không regression nào phát hiện trên toàn bộ codebase.

## 🎯 Đ8 — Vá P0 (proactive mutate mù) + phát hiện remote-agent không tái dùng được cơ chế thực thi hiện có

Tiếp nối câu hỏi thẻ Telegram `[AUTO-FIX-LEARNING] deployment_not_found deployment='<valid_deployment>'`
ở cuối Đ7. Xác nhận: `_resolve_from_action_experience` (proactive_observer.py) thực thi
`fn(ctx, args)` với `args` lấy verbatim từ payload RAG — không hề kiểm placeholder hay
phạm vi host. K8s tự chặn lần đó vì tên không tồn tại — MAY MẮN, không phải nhờ gate.

### Đã sửa — module dùng chung `src/pkg/reasoning/known_fix_resolver.py` (mới)

Tách logic search+validate+execute ra khỏi `_resolve_from_action_experience`, thêm 2 lớp
kiểm độc lập với điểm similarity trước khi cho thực thi:
1. `_has_placeholder` — chặn args có token `<...>` chưa điền (case gây lỗi thật).
2. `_out_of_scope` — nếu caller truyền `host_scope` (tập identifier CÓ THẬT của host/mục
   tiêu), args tham chiếu resource ngoài tập đó bị chặn. `host_scope=None` chỉ tắt lớp
   này, không tắt lớp (1) — dùng khi caller (proactive cluster) chưa có cluster-inventory
   context để đối chiếu.
3. Không dừng ở ứng viên top-1 nếu bị từ chối — thử tiếp ứng viên xếp sau (tăng độ chính
   xác thay vì bỏ cuộc ngay khi top-1 là rác).

`proactive_observer._resolve_from_action_experience` nay chỉ là wrapper mỏng gọi
`resolve_known_fix()` (giữ nguyên chữ ký + `_is_negative_pattern` guard cũ). Test mới
`tests/test_known_fix_resolver.py` (11 test, TDD đỏ→xanh) + toàn bộ 257 test cũ của
proactive_observer/react_runner/track2b vẫn xanh không đổi.

**2 lần tự sửa sai trong lượt này (ghi lại vì user coi trọng độ chính xác):**
- Nói "làn proactive hoàn toàn không có LLM" — SAI, bỏ sót `run_proactive_react_fallback`
  (`proactive_react_runner.py:44`, có `inc_llm_requests()`) — luồng thật là SOP → memory-
  recall (không LLM, đây mới là chỗ hở) → governance gate → LLM fallback nếu được phép.
- Gọi `_parse_fallback_tool_call` là "dead code" — SAI, chỉ grep trong 1 file nên bỏ sót
  lệnh gọi chéo từ `proactive_react_runner.py:138`. Hàm này sống, là core của bước LLM
  fallback ở trên, có 20+ test phủ trong `test_cov_proactive_react_runner.py`.

### CHƯA làm — remote-agent cần kiến trúc dispatch KHÁC, không tái dùng được

User yêu cầu remote-agent (VM khách, không có Prometheus) cũng phải có phản xạ nhanh như
proactive cluster, trigger bằng chính deviation z-score/ngưỡng tĩnh đã tính sẵn trong
`knowledge_pipeline._decide_and_promote()` (không cần cào Prometheus phía khách — đúng
điểm nối tự nhiên, xem `_decide_metric_deviation`/`_promote_to_anomaly` dòng ~147-330).

**Phát hiện chặn đường tắt:** `resolve_known_fix()` thực thi bằng gọi hàm Python tại chỗ
(`TOOL_REGISTRY[tool](ctx, args)`) — mô hình CHỈ đúng cho tài nguyên trong cụm (K8s API
gọi trực tiếp). `PROACTIVE_MUTATE_TOOLS` xác nhận điều này: cả 4 tool đều là K8s
(`k8s_rollout_restart`, `k8s_scale_deployment`, `k8s_patch_resource`, `kubectl_cluster`).
Mutation trên VM khách (systemd unit) phải qua KÊNH LỆNH BỀN (gateway HTTP enqueue → agent
tự poll → thực thi → báo kết quả qua `remote_command_outcome_loop.py`), đúng mô hình
`auto_recovery_bridge.py` đang dùng cho vòng ReAct — không phải lời gọi hàm đồng bộ.

Nghĩa là: phần SEARCH + 2 LỚP GUARD của `resolve_known_fix` tái dùng được nguyên xi cho
remote-agent (đưa `host_scope` từ `discovery.load_discovery_snapshot(agent_id=...)` —
snapshot có sẵn `services[].name`, dạng bare không có hậu tố `.service`). Phần THỰC THI
thì phải viết mới theo mô hình dispatch-bất-đồng-bộ-rồi-chờ-kết-quả, không phải
`fn(ctx, args)` tại chỗ.

### ĐÃ LÀM TIẾP TRONG CÙNG LƯỢT — dispatch remote-agent qua kênh lệnh bền

1. **Tách `known_fix_resolver.py`** thành `find_known_fix_candidate()` (search + 2 lớp
   guard, KHÔNG thực thi, nhận `valid_tools` để đổi universe tên tool) và `resolve_known_fix()`
   (wrapper cũ, thực thi tại chỗ cho cluster — hành vi không đổi, 268 test cũ vẫn xanh).
2. **Module mới `src/workers/remote_known_fix.py`** — nối `find_known_fix_candidate` (với
   `valid_tools=auto_recovery_bridge._SUPPORTED_CAPABILITIES`, KHÔNG phải `TOOL_REGISTRY`,
   vì đây là 2 universe tên hoàn toàn khác nhau: `systemd.restart_unit` vs `k8s_rollout_restart`)
   với `auto_recovery_bridge.dispatch_if_eligible()` — tái dùng nguyên xi cơ chế dispatch đã
   có sẵn CRAT fail-closed + allowlist blast-radius (`OMNI_LAB_AUTO_EXECUTE_AGENTS`) +
   đăng ký reconcile cho `remote_command_outcome_loop`. Không viết dispatch mới, không mở
   thêm bề mặt rủi ro nào ngoài những gì `dispatch_if_eligible` đã kiểm.
3. **Gắn vào `knowledge_pipeline._decide_and_promote()`** — hàm mới `_try_known_fix_reflex()`:
   trước khi nâng một deviation lên ANOMALY (kéo cả vòng RAG+LLM), thử tìm cách sửa đã biết,
   `host_scope` lấy từ discovery snapshot thật của agent đó (chấp nhận cả dạng bare lẫn
   `.service` vì không chắc quy ước của bản ghi cũ trong `action_experience`). **Không có
   snapshot ⇒ không mạo hiểm, đi đường đầy đủ như cũ** (fail-closed về hành vi trước khi có
   tính năng này, không phải fail-open).
4. Test mới: `test_known_fix_resolver.py` (11), `test_remote_known_fix.py` (6),
   `test_knowledge_pipeline_known_fix_reflex.py` (5) — tổng 350 test trên toàn bộ bề mặt đã
   đụng, xanh hết. Suite đầy đủ đang chạy lại lần cuối để xác nhận không có regression ẩn
   ở chỗ khác.

**Còn thiếu (chưa làm, không nằm trong yêu cầu lượt này):** vòng học của remote-agent —
hiện KHÔNG có gì ghi `action_experience` với `tool="systemd.*"` khi một recovery qua vòng
ReAct đầy đủ thành công (`remote_command_outcome_loop.reconcile_one` chỉ ghi CRAT + publish
feedback, không upsert vector store). Nghĩa là cơ chế reflex này ĐÃ SẴN SÀNG chạy nhưng
collection có thể trống rỗng cho capability remote — nó chỉ có ích từ lần một bản ghi thật
xuất hiện. Đóng vòng học (hook save-on-success vào `reconcile_one`) là việc tiếp theo hợp lý
nhưng KHÔNG được yêu cầu trong lượt này — cố tình để lại, không lặng lẽ bỏ qua.

### Việc CHƯA làm khác, mang từ Đ7 sang (chưa động tới trong Đ8)

- P1: `is_complete` chỉ đếm `turn_n >= _MIN_TURNS`, không đếm đã chạy lệnh thật chưa —
  case thật `ra-b5db53b7dc24` hoàn tất confidence 0.95 với `commands_requested: []` cả 2
  lượt dù agent online (`diagnosis_loop.py:900,914`).
- P1: `_apply_grounding_gate` không kiểm `affected_components`/`blast_radius` — 19 service
  lọt nguyên vào remediation của case trên (`diagnosis_loop.py:292-355`, call site 975-976).
- P2: `remote_command_outcome_loop.drain_once` không khoá nguyên tử trước khi xử lý — MEDIUM,
  chưa lộ vì `omni-fullstack` đang `replicas=1`.
- Carried: gauge `omni_kafka_consumer_lag` không tự hết hạn (`omni_worker.py`
  `_report_kafka_lag`) — mới workaround bằng restart pod, chưa sửa code.
- Carried: drift `OMNI_TELEGRAM_POLLING_ENABLED` — Deployment override `true` vs
  configmap/git `false`, chưa quyết hướng nào là đúng.

## 🎯 Đ7 — 4 sub-agent phản biện + thiết kế, đổi model qwen2.5→qwen3.6:27b

User phản biện 2 điểm tôi nêu ở Đ6: (1) LLM bịa trong SIEM là do **system prompt kém**,
không phải lý do cấu trúc để cấm LLM vĩnh viễn; (2) state-contrast dựa trên **bằng chứng
đã kiểm chứng thật**, LLM nên lý luận TRÊN nó chứ không thay thế nó. Mục tiêu: **mọi sự
cố đi qua MỘT luồng LLM+RAG duy nhất**. Bật 4 sub-agent song song (2 phản biện, 2 thiết
kế) + tự tổng hợp (agent tổng hợp riêng từng chết vì hạn mức phiên ở lượt trước).

### Agent 1 — Phản biện 6 fix Đ6 (diagnosis_loop.py): 3 lỗi HIGH, ĐÃ SỬA

| # | Lỗi | Sửa |
|---|---|---|
| HIGH | `semaphore.release()` ném lỗi (Redis rpush lỗi mạng thoáng qua) không có try/except riêng ⇒ exception thoát thẳng khỏi `run_diagnosis_loop`, `redis.set(session)` cuối hàm **không bao giờ chạy** — vi phạm INV_DIAG_STORED âm thầm, mất cả kết luận ĐÚNG đã có ở lượt 1-2 | Bọc `try/except` quanh `release()`, log lỗi, không re-raise. Slot có thể rò (quan sát được qua metric) nhưng phiên không mất trắng |
| HIGH | `_fallback_remediation_steps` không có nhánh CPU — chính ca thật `ra-1d897ff0cc93` (root_cause khôi phục bởi `_best_turn` là "CPU saturation") rơi vào catch-all `df -h/free -h` không liên quan | Thêm nhánh `cpu/load average/load_avg/saturation` → `top -b -n 1`, `ps --sort=-%cpu`, `uptime` |
| HIGH | Phiên 0-lượt vì hết slot LLM (semaphore busy) không phân biệt được với phiên thật-nhưng-rỗng ở `degraded`/`degraded_reason` — downstream (CRAT/KPI) không tự suy ra được, đúng loại tín hiệu quan trọng nhất mà bộ đo 46% lỗi hạ tầng cần | Thêm cờ `semaphore_bailout`, gộp vào `degraded_reason` (giữ nguyên chuỗi `"agent_offline: ..."` cũ, không phá test có sẵn) |

4 test mới (`test_diag_loop_context_fidelity.py`, TDD đỏ→xanh) + 3 finding MEDIUM/LOW
được ghi nhận nhưng chưa sửa (làn `reactive` không dành riêng cho diagnosis_loop; test
chưa mô phỏng concurrency thật bằng semaphore thật; `_parse_error` không được xử lý đối
xứng `is_infra_error` khi nhét vào lịch sử hội thoại). **18/18 test xanh sau sửa.**

### Agent 2 — Phản biện Đ1-Đ5 + UI: 2 MEDIUM, chưa sửa

- **`VLLMClient.embed()` hoàn toàn không được instrument** (`vllm_client.py:474-492`) —
  mâu thuẫn tuyên bố Đ2 "instrument tất cả lời gọi LLM". Một lỗi embedding sẽ không xuất
  hiện ở `omni_llm_calls_total` hay Tempo.
- **`remote_command_outcome_loop.drain_once` không khoá** — nếu `omni-fullstack` scale
  >1 replica (hiện 1, chưa lộ), cùng `command_id` có thể bị `reconcile_one` xử lý 2 lần
  trước khi `zrem`, ghi CRAT trùng + publish `omni-action-feedback` trùng.
- Đ1, Đ3 không có vấn đề đáng kể. UI `/architecture`: `diagrams.ts` hardcode nhưng tự
  khai trung thực (có `MEASURED_AT` + bảng nguồn xác minh) — không phải bug che giấu.

### Agent 3 + 4 — Thiết kế "một luồng duy nhất": phát hiện quan trọng hơn cả đề bài

**Phát hiện lớn nhất (agent 4): `OS_STATE_CONTRAST` KHÔNG PHẢI so sánh trạng thái —
đó là RAG.** Nó gọi `run_os_diagnostic_loop` → `similarity_search(score_threshold=0.55)`
rồi trả nguyên văn `root_cause/fix` từ payload vector match, nhưng `mark_stage(RAG,
"skip")` nói dối là không dùng RAG. Trong khi đó, hàm so sánh trạng thái OS THẬT
(`compare_alert_claim_to_os_state`, `os_state_validator.py:620`) có **0 call site** —
dây chết. Chỉ `STATE_MACHINE_CONTRAST` (K8s, `alert_sdk_truth_compare.py:139`) là phép
so trạng thái thật (kubelet/Metrics API) — nhưng nó **nấu bằng chứng thành văn xuôi
ngay tại nguồn** (trả `str`, không trả cấu trúc), nên "đưa vào làm evidence có cấu trúc"
đòi phải tách hàm trước.

**Phát hiện lớn của agent 3**: `advisory_mode_system_prompt.py` đã có sẵn `_SIEM_SECTION`
+ `_SIEM_TRIGGERS` — **code chết**, không trace SIEM nào chạm tới vì nhánh deterministic
`evidence_consumer.py:2600` return trước. Và chính đoạn prompt chết đó đang RA LỆNH gây
bịa: *"Steps MUST cover ≥1 kubernetes ... network-only steps violate the framework"* —
với DDoS từ IP ngoài cụm, prompt **bắt buộc** model sinh một bước K8s dù không có
workload nào để chỉ. Xác nhận trực tiếp giả thuyết của user: lỗi ở prompt, không ở LLM.
Luồng đích **không phải** `diagnosis_loop.run_diagnosis_loop` (đó là Linux bare-metal,
sai miền) mà là nhánh advisory sẵn có ở `evidence_consumer.py:2669-3090` — đã có RAG,
System Twin, grounding gate, VERIFY read-only, CRAT, HITL, chỉ thiếu 2 việc: (a) evidence
SIEM đưa vào làm "CONFIRMED", (b) một block "CLUSTER INVENTORY" thật để chặn bịa tên
resource — gate hiện tại (`_KIND_NAME_RE`) bị agent đo thủng ở nhiều dạng câu và **không
có lớp claim nào cho IP/CIDR** (rủi ro cao nhất: bịa IP trong luật chặn firewall).

Rủi ro chung cả hai thiết kế: **mở đường mutate lên đúng loại ca không nên mutate**
(SIEM: workload trong cụm không tồn tại; state-contrast: workload đang khoẻ) — cả hai
thiết kế đều đề xuất khoá `suggested_recovery=None` cưỡng chế bằng GATE (không phải chỉ
prompt) cho hai luồng này. Cả hai đều đề xuất giữ nhánh deterministic làm **fallback khi
LLM lỗi**, không xoá hẳn — advisory là bổ sung/thay thế có kiểm soát, không phải cắt dây
an toàn.

**Việc CHƯA làm** (đây là thiết kế, có kế hoạch P0→P4/8 bước chi tiết trong báo cáo đầy
đủ của 2 agent, chưa viết code): sửa 2 nhánh trước khi gộp (score/confidence cứng ở
OS contrast, `mark_stage` nói dối), viết `build_state_comparison_block()` +
`apply_state_comparison_gate()` (đảo verdict), `build_siem_confirmed_block()` +
`build_cluster_inventory_block()` + 6 luật system prompt + G1-G4 gate (IP/CIDR trước
tiên). **Quyết định kiến trúc cần user duyệt trước khi code**: có chấp nhận độ trễ tăng
(2 lượt LLM thêm/sự cố, xếp hàng qua semaphore vốn đã bão hoà) đổi lấy đồng nhất hoá,
hay cần "thẻ hai nhịp" (deterministic tức thì + LLM enrichment nền, agent 3 đề xuất)?

### Đổi model: qwen2.5-coder:7b → qwen3.6:27b

Đo thật trước khi đổi: model tồn tại trên Ollama host (27.8B, Q4_K_M, context 262144,
capabilities `vision/completion/tools/thinking`), gọi thật thành công (91.4s cho 20
token cold-load, `error=None`). Đổi 7 biến trong `k8s/deployments/omni-worker-configmap.yaml`
+ 4 default Pydantic (`settings.py`) + 2 fallback (`diagnosis_loop.py`,
`remote_agent_pipeline.py`). Apply configmap + `kubectl rollout restart` cả
`omni-fullstack` và `omni-onboarding` (cả hai đọc chung configmap) — **thành công**, env
pod xác nhận `qwen3.6:27b` ở cả 6 biến model. Test suite 7312 xanh trước và sau đổi.

⚠️ **Chưa đo được độ trễ một lượt chẩn đoán thật (`num_predict=1024`)** — `kubectl exec`
bị TLS-handshake-timeout tới API server 2 lần liên tiếp khi thử (hạ tầng exec channel,
không phải model). Model có capability `"thinking"` — có thể tốn budget suy luận trước
khi ra JSON, đúng dạng rủi ro làm nặng thêm chính cơn bão đồng thời vừa sửa ở Đ6 nếu một
lượt thật > 120s (`llm_chat_timeout_sec` mặc định). **Cần đo bằng LLM observability (Đ2)
trên tải thật** trước khi coi việc đổi model là an toàn hoàn toàn — chưa làm.

### ⚠️ REVERT ngay trong lượt: qwen3.6:27b → qwen3:8b (2026-08-03)

**qwen3.6:27b đã gây nghẽn máy thật, đo được, không phải suy đoán.** Sau khi restart 2
deployment trỏ qwen3.6:27b, `llama-server` (PID đổi 83148→6697, tức Ollama đã tự
reload/restart process — khả năng cao do traffic THẬT từ chính pipeline chẩn đoán Omni
gọi vào, không chỉ 2 lệnh test của tôi) chiếm **304%→227% CPU, 14.4GB RAM (57% RAM máy)**
liên tục >1 giờ. Hậu quả đo được: **full test suite local (bình thường 175s) không xong
nổi trong 600s (10 phút), 3 lần liên tiếp bị hệ thống kill.** User được hỏi 4 lựa chọn
xử lý tiến trình treo, nhưng chọn phương án khác: **"đề xuất 1 model khác phù hợp hơn"**.

**Đã đổi sang `qwen3:8b`** (đã có sẵn trên Ollama host, không cần pull — xem `ollama
list` trong log phiên: 8.2B, Q4_K_M, context 40960, capability `tools`+`thinking`,
footprint gần với `qwen2.5-coder:7b` cũ 7.6B). Đã sửa 7 biến configmap +
4 default Pydantic (`settings.py`) + 2 fallback (`diagnosis_loop.py`,
`remote_agent_pipeline.py`) — **cùng những vị trí đã sửa lần đổi qwen3.6:27b trước đó,
xem diff, đừng tưởng nhầm là lần đầu**. Đã `kubectl apply` configmap + restart 2
deployment.

**CHƯA XÁC NHẬN xong khi phiên dừng:**
- Rollout `omni-fullstack`/`omni-onboarding` sau đổi sang qwen3:8b — lệnh
  `kubectl rollout status` bị đưa xuống nền do máy vẫn chậm (chưa thấy log hoàn tất).
- **RAM của `llama-server` cũ (14.1GB) vẫn CHƯA giải phóng** dù CPU đã tụt về 0.3% —
  process (PID 6697, model qwen3.6:27b) còn resident, chỉ hết compute. Ollama có thể tự
  unload sau keep-alive timeout, hoặc cần restart `Ollama.app` thủ công nếu không tự
  giải phóng — CHƯA xác minh cái nào xảy ra.
- Chưa xác nhận `env` trong pod đã đổi thật sang `qwen3:8b` (làm y hệt bước đã làm cho
  qwen3.6:27b: `kubectl exec ... -- env | grep -E "VLLM_MODEL|OMNI_MODEL_"`).
- Chưa gọi thử thật `qwen3:8b` qua Ollama để xác nhận load+trả lời được (bước đã làm
  cho qwen3.6:27b nhưng CHƯA làm cho model mới).
- **Chưa chạy lại full test suite** để xác nhận 7312 pass sau đổi model lần 2 — 3 lần
  chạy liên tiếp trước đó đều bị kill do máy nghẽn, không phải do code sai.

**Việc tiếp theo khi resume:** (1) xác nhận RAM đã giải phóng hoặc restart Ollama.app
thủ công, (2) xác nhận rollout xong + env pod đúng qwen3:8b, (3) gọi thử thật qwen3:8b,
(4) chạy lại full test suite, (5) nếu máy vẫn chậm hơn baseline gốc (qwen2.5-coder:7b),
cân nhắc tiếp tục theo dõi qua LLM observability thay vì đổi model lần 3.

### ✅ ĐÃ XÁC MINH XONG — qwen3:8b hoạt động, máy đã hồi phục

- **RAM giải phóng đúng như dự đoán**: sau restart 2 deployment, `llama-server` cũ
  (14.4GB, blob `83c54730...`) biến mất khỏi `ps aux`, Ollama tự load blob `a3de86cd...`
  (qwen3:8b, chỉ 9.8GB RAM, 4.5% CPU) — không cần can thiệp thủ công.
- **Full test suite: 7316 passed, 175.17s** — đúng bằng baseline gốc (trước khi đụng
  vào model 27B), xác nhận máy đã hồi phục hoàn toàn.
- **Phát hiện thêm, đã xử lý**: sau khi máy nhẹ lại, gọi thử `qwen3:8b` qua
  `/api/generate` và `/api/chat` vẫn TREO >30-180s dù CPU llama-server gần như rảnh
  (3.2%, chỉ 17s CPU tích luỹ) — bất thường vì `/api/tags` (endpoint nhẹ, không qua
  hàng đợi inference) vẫn trả về tức thì (5ms). Chẩn đoán: hàng đợi request của Ollama
  (`-np 1`, chỉ 1 slot xử lý song song) bị **kẹt bởi chính các lệnh test của tôi bị ngắt
  ngang phía client** (curl timeout/exec bị kill) mà server không phát hiện client đã
  bỏ cuộc — request zombie chiếm slot duy nhất vĩnh viễn. User xác nhận cho restart
  Ollama.app (`killall Ollama` + `pkill llama-server` + `open -a Ollama`) — **sau
  restart, gọi thử thành công ngay: HTTP 200, `qwen3:8b`, 14.33s cho 112 token
  (bao gồm cả "thinking"), kết luận đúng "four"**. Đây là bằng chứng quan trọng: **14.3s
  nằm thoải mái trong ngân sách `llm_chat_timeout_sec=120s` mặc định** — khác hẳn rủi ro
  đã cảnh báo cho qwen3.6:27b (91s cho chỉ 20 token).

**Kết luận đổi model: HOÀN TẤT VÀ AN TOÀN.** `qwen3:8b` là lựa chọn đúng — nhẹ, nhanh,
tương thích ngân sách timeout hiện tại, không gây tranh chấp tài nguyên trên máy dùng
chung K8s+Redis+nhiều pod. Bài học cho phiên sau: **một request client-side bị kill/
timeout không đảm bảo server-side generation dừng theo** khi Ollama chạy `-np 1` — nếu
gặp lại hiện tượng "CPU rảnh nhưng generate treo vô hạn", nghi ngay hàng đợi kẹt, không
phải model chậm; restart Ollama.app là cách xử lý rẻ nhất.

### 🔬 Câu hỏi phụ: "LLM có bị nạp lại model mỗi request như F5-TTS không?" — KHÔNG

Đo trực tiếp, không suy đoán:
- `GET /api/ps` → `expires_at` ≈ 5 phút sau giờ hiện tại, đúng `OLLAMA_KEEP_ALIVE` mặc
  định. `vllm_client.py` nhận tham số `keep_alive` nhưng **không set** (comment "accepted
  for call-site compat, unused") — luôn dùng default server-side.
- Gọi thật sau restart Ollama: `load_duration=93.5ms` (không phải giây) — không phải cold
  load. `eval_duration` (suy luận thật) chiếm gần hết `total_duration`.
- Log production thật (`llm_observability`): 2 lượt liên tiếp 14.4s và 15.9s, nhất quán —
  không có dấu hiệu nạp lại giữa các lượt.

### ⚠️ Phát hiện phụ nghiêm trọng hơn cả câu hỏi gốc: gauge `omni_kafka_consumer_lag`
### STALE (kẹt giá trị cũ), gây crash-loop thật trên `omni-fullstack` VÀ `omni-onboarding`

Trong lúc kiểm tra, phát hiện `omni-fullstack` đang **CrashLoopBackOff** (9 restart).
`/healthz` báo `kafka_lag: unhealthy, lag=10308` → liveness kill lặp lại. Điều tra bằng
`kafka-consumer-groups.sh --describe --all-groups` trên broker thật (nguồn sự thật, không
phải metric nội bộ) cho thấy **TẤT CẢ consumer group của omni-fullstack lag=0** — không hề
có backlog thật. Root cause: `_report_kafka_lag()` (`omni_worker.py:622-636`) set Gauge
`omni_kafka_consumer_lag` (label theo `topic,consumer_group`) mỗi khi commit message; Gauge
**không tự decay** — nếu một topic/group từng có lag cao dù chỉ một lần (khả năng cao do
chính 3 lần tôi restart pod trong phiên này gây rebalance/backlog thoáng qua) rồi topic đó
ngừng nhận message mới, giá trị CŨ ở lại **vĩnh viễn** trong registry vì không ai gọi lại
`set_kafka_consumer_lag` cho đúng label đó để hạ xuống. `_read_check_states()` lấy `max()`
qua mọi sample từng thấy từ lúc process khởi động — nên một đỉnh lag thoáng qua ám ảnh
health check mãi mãi cho tới khi process restart (Gauge reset về không có sample).

**Cùng lớp bug với `omni_kpi_advisory_acceptance_rate` đã sửa ở Đ1 (memory
`project_no_ground_truth_root_cause`)** — khác chiều: KPI gauge kẹt THẤP giả (0.0 nhìn
như "kém" trong khi là "chưa biết"), còn lag gauge này kẹt CAO giả (10308 nhìn như "đang
tắc" trong khi thực tế lag=0). Cả hai đều là hệ quả của việc dùng Prometheus Gauge làm bộ
nhớ "giá trị cuối" mà không có cơ chế hết hạn/reset khi điều kiện đổi.

**Đã xử lý**: xoá pod crash-loop (`kubectl delete pod`) để buộc tạo pod mới — Gauge reset
sạch, `/healthz` báo `kafka_lag: ok, lag=0` ngay từ giây đầu, pod ổn định 1/1 Ready.
`omni-onboarding` cũng dính cùng bug (13 restart/71m), tự phục hồi tương tự sau restart
tự nhiên của chính nó — hiện `status=degraded` (chỉ vì tạm chưa có message mới, KHÔNG phải
`unhealthy`, không bị kill tiếp).

**KHÔNG cần dọn Kafka/Redis như dự kiến ban đầu** — không có backlog thật nào để xoá.
`omni-onboarding-discovery` (group RIÊNG, pod `omni-onboarding`, KHÁC `omni-fullstack`) có
lag THẬT nhưng nhỏ và đang tự rút đều (7567→3551 trong vài chục giây), không crash-loop,
không cần can thiệp — xoá offset ở đây sẽ MẤT THẬT dữ liệu discovery evidence mà không có
lợi ích gì vì nó đang tự xử lý bình thường.

**Việc CHƯA làm, cần theo dõi tiếp**: sửa gốc `_read_check_states()`/`observability_metrics_loop`
để lag health check phản ánh **giá trị mới nhất per-topic**, không phải `max()` từ đầu
process — nếu không, bug này sẽ tái diễn ở lần restart tiếp theo bất cứ khi nào có backlog
thoáng qua thật (kể cả bình thường, không do tôi gây ra). Full test suite: **7316 passed,
176.55s** sau khi cả 2 pod ổn định.

## ✅ Đ6 — Vòng ReAct chẩn đoán: 46% lượt là lỗi hạ tầng, không phải phình ngữ cảnh

Đo trên **32 phiên THẬT** trong Redis (`omni:diag:session:*`), không phải suy luận:

```
tổng lượt              185
lượt lỗi LLM             85   ← 46% (timeout LLM, không phải lỗi model)
phiên cạn 8 lượt         20/32
phiên kết luận «llm_error» dù lượt 1 đã chẩn ĐÚNG   6   ← root cause 1
lượt lệnh bị dedup âm thầm rồi model hỏi lại y hệt   25  ← root cause 2
"context budget exceeded" trong log                  0   ← BÁC BỎ giả thuyết phình ngữ cảnh
```

**Nguyên nhân thật: BÃO ĐỒNG THỜI, không phải phình ngữ cảnh.** 18 phiên 8-lượt chồng
lên nhau trong đúng 14 phút (10:37→10:51 02/08), tất cả cùng đập vào MỘT model 7B.
`LLMSemaphore` với làn `reactive` riêng đã tồn tại sẵn trong repo (`llm_semaphore.py`)
nhưng **`acquire_reactive()` có 0 call site** trong toàn bộ `src/` trước bản vá này —
vòng chẩn đoán remote-agent gọi thẳng `llm_client.chat()`, đi vòng qua cơ chế chống bão
đã dựng sẵn cho chính nó.

**6 khiếm khuyết đã sửa** trong `src/services/analyst/diagnosis_loop.py`
(test: `tests/test_diag_loop_context_fidelity.py`, 14 test, TDD đỏ→xanh):
1. **`_best_turn()`** — kết luận lấy từ lượt CÓ CONFIDENCE CAO NHẤT, không phải
   `turns[-1]`. Ca thật `ra-1d897ff0cc93`: lượt 1 chẩn đúng "CPU saturation" conf=0.85,
   nhưng `final.root_cause="llm_error" conf=0.0` vì lượt cuối tình cờ là lượt lỗi.
2. **Lỗi hạ tầng không còn bị nhét vào `messages` như một lời trợ lý** — trước đây
   `messages.append({"role":"assistant","content": "...llm_error..."})` khiến lượt sau
   model đọc lại chính "câu trả lời" lỗi của mình.
3. **Ngắt mạch sau 2 lỗi liên tiếp** (`_MAX_CONSECUTIVE_LLM_ERRORS`) — trước đây một
   phiên có thể chạy đủ 7 lượt lỗi × timeout 120s (~14 phút) trong khi mỗi lượt lại đập
   thêm vào đúng con model đang quá tải.
4. **`_build_followup_context(..., suppressed=...)`** — lệnh bị dedup nay được nói RÕ
   lý do thay vì im lặng thành `"(no commands were dispatched)"`. Ca thật
   `ra-7be04b7fb43e`: 7/8 lượt bị dedup, model hỏi lại y hệt tới khi cạn lượt.
5. **`_enforce_context_budget` trừ `num_predict` trước khi tính ngân sách** — `num_ctx`
   là cửa sổ CHUNG prompt+completion; không trừ phần model sắp sinh ra thì Ollama vẫn có
   thể cắt ĐẦU (mất system prompt) ngay cả khi ngân sách coi là "vừa".
6. **Nối `LLMSemaphore.acquire_reactive()`** vào từng lượt — `semaphore` là tham số
   optional (không phá call site cũ), truyền thật ở `remote_agent_pipeline.py` qua
   `ctx.semaphore`.

**Xác minh:** `tests/test_diag_loop_context_fidelity.py` 14/14 xanh; suite liên quan
(diag/remote-agent/pipeline) 150/150 xanh; **toàn bộ 7312 test xanh, 0 fail**.

### Hai bất biến user yêu cầu kiểm — kết quả

**"Ngữ cảnh phải độc lập theo từng sự cố"** → **ĐÚNG, đã có sẵn, xác minh lại**: mọi
biến trong `run_diagnosis_loop` (`messages`, `turns`, `evidence_corpus_parts`) là local
theo lần gọi; session Redis khoá theo `trace_id`; kết quả lệnh khoá theo `cmd_id` UUID
duy nhất dù hàng đợi lệnh dùng chung theo `agent_id`. Không có biến module-level mutable
nào (đã grep). Không rò/pha trộn giữa các sự cố.

**"Mọi sự cố phải đi qua RAG/LLM"** → **CÓ NGOẠI LỆ THẬT, cố ý, đã có audit trail** —
cần user quyết định, không tự sửa:
- **SIEM (F25, `evidence_consumer.py:2588-2645`)**: mọi batch SIEM đi thẳng
  `_siem_diagnosis_from_batch()` — template theo category + rule-engine
  (`reason_why/reason_blast_radius/reason_verify`), `mark_stage(...,"LLM","skip",
  detail="siem kill-chain — deterministic, no LLM")`. Lý do ghi trong code: planner LLM
  từng bịa ra pod đích giả ("nginx-ingress-controller-…") cho một DDoS từ IP ngoài cụm —
  không có workload nào trong cluster để mutate, LLM cứ phải bịa ra một cái.
- **STATE_MACHINE_CONTRAST/OS_STATE_CONTRAST** (`evidence_consumer.py:2178-2194`): so
  trạng thái trước/sau thuần deterministic, cũng `LLM=skip`, cũng tự viết CRAT riêng
  (giống pattern SIEM).
- Các skip còn lại (`sigma_gate_suppressed`, `meta_self deduped`, urgency dưới ngưỡng ở
  `remote_agent_pipeline.py:342`) đều là trường hợp **KHÔNG PHẢI sự cố thật** (false
  positive theo 3σ, noise tự-giám-sát, độ khẩn thấp) — đúng theo INV_KNOWLEDGE_NOT_ALERT,
  không phải ngoại lệ cần sửa.
- ⚠️ **Xung đột thật với yêu cầu vừa nêu**: nếu "mọi sự cố" bao gồm cả SIEM/state-contrast,
  hai đường trên phải đổi. Đây là quyết định kiến trúc (rủi ro LLM bịa mục tiêu ngoài
  cụm vs. tính đồng nhất "mọi sự cố qua LLM"), không phải bug — CHƯA tự sửa, chờ quyết
  định.

## 🔄 SUB-AGENT ĐANG CHẠY — working tree BIẾN ĐỘNG LIÊN TỤC

**Ảnh chụp 16:46 02/08:**
- **94 file** ở trạng thái `M`/`??` (16:20 là 77 → đang tăng vì agent viết liên tục).
- **Bộ test đã thu thập lại được**: `7272/7283 collected, 0 error`. Agent 3 đã vá module
  `pkg/diagnostics/measurement_grounding` bị thiếu ⇒ tình trạng vỡ ở khâu collect **đã hết**.
- **Cả 5 agent ĐÃ XONG.** Suite toàn bộ: **7298 passed, 0 failed** (baseline 7189).
- **Agent 6 (tổng hợp) CHẾT vì hạn mức phiên 100%** (reset 21:39 02/08). Chủ phiên tự tổng
  hợp thay, kết quả ở mục "TỔNG HỢP CUỐI" dưới đây.

## 📊 TỔNG HỢP CUỐI — đo lúc ~17:1x, sau khi cả 5 agent xong

### Đo chốt
```
Phễu trace  : 801 trace, TẤT CẢ chỉ có EVIDENCE (toàn onboarding discovery)
gw-prom-*   : 0        ← trước là 13. KHÔNG còn sinh cảnh báo tự-thân nào
Alert active: 0        ← trước là 4, gồm OmniAdvisoryAcceptanceLow
omni:kpi:*  : rỗng     ← chưa có mẫu phán quyết THẬT nào
playbook_graduation: 0 rows
```

### Giải mâu thuẫn agent 4 ↔ agent 5: **CẢ HAI ĐÚNG, hai chỗ vỡ NỐI TIẾP nhau**
- **Vỡ #1 (agent 4)** — chuỗi gate ở gateway trả HTTP 423 thật. Bằng chứng log:
  `event=auto_recovery_dispatched ... http=423`. Lệnh **không được phát đi**.
- **Vỡ #2 (agent 5)** — kể cả khi lệnh phát đi được, **kết quả không bao giờ quay về**:
  gateway kết thúc trong máy trạng thái riêng, không mark `EXECUTOR`/`FEEDBACK`, không
  publish `omni-action-feedback`, không ghi CRAT.
- **Bằng chứng cho cách đọc này**: diễn tập của agent 5 phải làm **CẢ HAI** mới chạy được —
  vừa mở cấu hình gate (runtime_flag + tier + kill-switch gateway) vừa viết
  `remote_command_outcome_loop.py`. Nếu chỉ có một chỗ vỡ thì một sửa đã đủ.
- ⇒ `0/809` là **hệ quả cộng dồn**, không phải một nguyên nhân duy nhất.

### Vòng tự nuôi: **ĐÃ CẮT** ở đầu vào
Chuỗi cũ: sự cố không đi hết luồng → không thẻ → sổ khen trống → điểm 0 → tự báo động →
chiếm pipeline. Nay: gauge **không còn series** khi chưa có mẫu ⇒ alert không fire ⇒
`gw-prom-*` = 0. **Mắt xích "điểm 0 giả → tự báo động" đã đứt.**
Sổ khen vẫn trống — nhưng nay là "chưa biết" TRUNG THỰC, không phải "0% giả do fixture cũ".

### SỐNG / NẰM IM / CHƯA XONG
**SỐNG** (có bằng chứng runtime): KPI gauge nói thật (0 series, 0 alert) · quan sát LLM
(71 dòng `event=llm_call`, dashboard `omni-llm`, span vào Tempo) · UI `/architecture` (200 ở
cả 2 mặt) · grounding gate `INV_DIAG_MEASURED` (replay 3 thẻ đúng cả 3) · `ps`/`top` đã chạy
`rc=0` trên VM với bundle CŨ · `remote_command_outcome_loop` (diễn tập thật, CRAT 182→183).

**NẰM IM** (code có, chưa có đường tới):
- **Vòng học** — 0 mẫu. Cần **người bấm nút phán quyết trên Telegram**. Không code nào thay được.
- **Quyết định auto-recovery** — đường ống thông, nhưng chưa chứng minh vòng chẩn đoán **tự
  sinh** `suggested_recovery` cho sự cố thật. Agent 5 dựng `final` bằng tay; agent 4 đo 9/12
  phiên `null`; chỉ có 3 capability; và grounding gate nay còn **drop** `suggested_recovery`
  khi kết luận chưa đo được (đúng, nhưng thu hẹp thêm đường).
  ⇒ **Omni chạy được lệnh; chưa chứng minh nó biết khi nào nên ra lệnh.**

**CHƯA XONG**: `OMNI_ENV_MODE` gateway=`prod` vs worker=`dev` · guard `AOIP_ALLOW_SELF_RESTART`
chưa có trên VM · `OMNI_RAG_GATE_ENABLED=false` · coupling `omni:autonomous:terminal:{trace}`.

### Việc tiếp theo, theo giá trị/chi phí
1. **Bấm nút phán quyết trên một thẻ Telegram** — rẻ nhất, và là mắt xích DUY NHẤT còn thiếu
   để vòng học có mẫu thật đầu tiên. Bằng chứng xong: `omni:kpi:z:*:accepted` có phần tử.
2. Thống nhất `OMNI_ENV_MODE` hai process, hoặc bỏ hẳn việc gate theo env_mode.
3. Quyết định về `OMNI_RAG_GATE_ENABLED=false` — hiện mọi lượt đốt LLM thật, không tra tri thức.
4. Diễn tập sự cố **khớp capability** (dừng `payment-api`) và để Omni **tự** sinh
   `suggested_recovery` — đây là thứ duy nhất chứng minh được "quyết định" chứ không chỉ "đường ống".
5. Roll bundle agent mới lên 3 VM để có guard self-restart.
6. Quyết định giữ hay revert quyền mutate (5 lệnh ở đầu file).
7. Commit — cả phiên chưa commit gì.

### ✅ AGENT 3 ĐÃ XONG — và nó SỬA NGUYÊN NHÂN GỐC TÔI ĐƯA RA

**Tôi sai lần thứ 3 cùng một lớp lỗi.** Tôi khẳng định "`apply_advisory_grounding_gate` không
được nối vào vòng chẩn đoán remote-agent" sau khi grep `remote_agent_pipeline.py` và
`os_diagnostic_loop.py` → rỗng. Nhưng gate nằm ở **`src/services/analyst/diagnosis_loop.py`**
— file tôi chưa bao giờ grep. Xác minh: `_apply_grounding_gate` ở `:262` (định nghĩa),
gọi tại `:848` và `:910`. **Grep quá hẹp rồi kết luận "không tồn tại" — đúng lớp lỗi
`positional_pairing_bug_class`/âm-tính-giả đã xảy ra 3 lần phiên này.**

**Vấn đề THẬT:** gate cũ neo vào *token* (đường dẫn, phần trăm, tên object). Câu
`"Insufficient memory available on the host"` lọt qua dễ dàng — không đường dẫn, không phần trăm.

**Lời giải:** thêm trục **trực giao** thay vì nhân đôi trục cũ — `INV_DIAG_MEASURED`:
*"có tool nào trong phiên này ĐO đại lượng đang được kết luận không?"*
Quyết định thiết kế đáng chú ý: **fact thu sẵn KHÔNG tính là phép đo**, vì mọi alert remote
đều kèm đủ cpu/mem/disk ⇒ đếm fact làm check vô nghĩa. `alert_hint` nêu đại lượng thật sự
bất thường; kết luận xoay sang chỗ khác thì phải có lệnh đo.

File mới: `src/pkg/diagnostics/measurement_grounding.py` (18.8 KB),
`command_normalize.py`, + 2 file test. Sửa: `diagnosis_loop.py`, `remote_agent_pipeline.py`,
`command_executor.py`, `agent_commands.py`.

**Replay 3 thẻ qua pod thật:**
| Thẻ | Kết quả |
|---|---|
| `ra-689e6dc59ea4` (sai — memory) | `[UNMEASURED: memory]`, confidence 0.95 → **0.3** |
| `ra-d645c49ed6d1` (sai — crash) | `[CONTRADICTED: systemctl is-failed rc=1]` + **drop `suggested_recovery`** — đây vốn là lệnh auto-restart nhắm vào unit ĐANG KHOẺ |
| `ra-da66cac8746b` (ĐÚNG) | `UNCHANGED` — **không dương tính giả** |

**Sửa `ps`/`top` chuẩn hoá phía PRODUCER ⇒ không cần deploy lại VM.** Chứng minh trên
`cust-app` với bundle CŨ (1.0.0): `ps rc=0` (trước: `unsupported option (BSD syntax)`),
`top rc=0` (trước: rc=1, rỗng). `exit_code` điền phía gateway từ `rc`.

**Đã áp 2 bản vá của agent 5** — xác minh: `redis=ctx.redis, kafka=ctx.kafka` có mặt ở
`remote_agent_pipeline.py:540-541` và `:600-601` ⇒ **auto-dispatch không còn nằm im.**
Đã làm luôn `push_trace_id` (việc chuyển từ agent 2).

**Cố ý KHÔNG làm (có lý do, đáng giữ):**
- **Không chặn Telegram.** Vô hiệu kết luận nhưng vẫn phát thẻ — cảnh báo dưới thẻ 1 là sự cố
  CPU 98% CÓ THẬT; chặn thẻ là bịt mắt. Marker + trần confidence + drop auto-recovery là
  trung thực mà không mù.
- Check confidence-inflation cố ý hẹp (chỉ bắt ca xoay-đại-lượng-mà-conf-tăng-không-có-đo);
  quy tắc tổng quát "conf tăng không có bằng chứng mới" sẽ bắn nhầm khi một lượt xác nhận
  hợp lệ giả thuyết cũ.
- Check mâu thuẫn dịch vụ có thể bắn nhầm với unit crash-loop nhưng restart thành công
  (`is-failed` đọc ra khoẻ) ⇒ vì thế phạt bằng **hạ confidence**, không xoá.
- Lane advisory vẫn chỉ chạy trục token — mở rộng cần `command_results` theo phiên mà lane đó
  không mang; ghi vào docstring thay vì giả vờ.

Hai test đổi contract (`test_no_suggested_recovery_skips_silently_no_stage_row`,
`test_eligible_but_no_gateway_key_skips_silently`): trước assert *không có stage row nào*,
nay assert `skip` + reason. Vẫn là `skip` chứ không phải `fail` nên không bóp méo error rate.

### ✅ AGENT 5 ĐÃ XONG — Omni ĐÃ TỰ SỬA MỘT DỊCH VỤ THẬT TRÊN VM

**Nguyên nhân gốc của `0/809 trace chạm EXECUTOR` — ở BƯỚC 9, không phải ở gate.**
Giao thức giao lệnh vốn đã chạy được; nhưng gateway **kết thúc kết quả trong máy trạng thái
của chính nó**: không ai mark `EXECUTOR`/`FEEDBACK`, không publish `omni-action-feedback`,
không ghi CRAT cho mutation. Nên chuỗi trông như chưa bao giờ chạy.

**File mới `src/workers/remote_command_outcome_loop.py`** (12.5 KB, wired vào role `full` tại
`omni_worker.py:1254-1257`) — rút work-list có chặn, ghi CRAT, mark `EXECUTOR`/`FEEDBACK`,
publish `omni-action-feedback`.
Sửa: `auto_recovery_bridge.py` (allowlist agent + **CRAT ghi TRƯỚC dispatch**, ledger hỏng ⇒
không phát lệnh), `src/aoip/agent/runtime_config.py` (guard self-restart), `omni_worker.py`.
Test mới: `tests/test_remote_auto_execute_loop.py` (22) + cập nhật `test_auto_recovery_bridge.py`.
**Suite: 7294 passed, 0 failed** (baseline 7189).

**Diễn tập thật — `systemctl stop payment-api` trên `cust-app`, Omni tự khởi động lại:**
```
DISPATCH  {"dispatched": true, "command_id": "cmd-faa5ed8bbd8b47a8", "state":"QUEUED", "http":200}
VM        ExecMainStartTimestamp=2026-08-02 17:00:03 +07  ExecMainPID=522693  active
EXECUTOR  state=COMPLETED unit=payment-api.service rc=0 reason="service + dependents verified"
FEEDBACK  status=ok   ← do consumer CÓ SẴN (autonomous_feedback_loop.py:766) ghi
                        ⇒ chứng minh vòng Kafka THẬT SỰ khép, không phải loop tự mark
CRAT      seq=182 ADVISORY_DISPATCHED source=auto_recovery_bridge        (TRƯỚC dispatch)
          seq=183 ADVISORY_DISPATCHED source=remote_command_outcome_loop state=COMPLETED rc=0
          hash-chain khớp: 182.block_hash == 183.prev_hash
```
**Tôi xác minh độc lập**: `payment-api` đang `active`, PID 522693, đúng mốc 17:00:03; chuỗi
audit phía VM `/var/lib/aoip/recovery-audit.jsonl` có `RECOVERY_PLANNED → BEFORE_STATE →
EXECUTED → COMPLETED` (seq 60-63 lúc tôi đo; agent báo 56-59 — số nhích vì có lượt chạy sau).
Dọn dẹp: `payment-api` để lại `active/enabled` (do Omni khôi phục, không phải tay người);
CRAT block giữ lại có chủ đích (sổ append-only).

**⚠️ 2 bản vá BẮT BUỘC nằm trong file agent 3 giữ — ĐÃ chuyển, chưa áp:**
1. `remote_agent_pipeline.py:555` — thêm `redis=ctx.redis, kafka=ctx.kafka` vào
   `dispatch_if_eligible`. **Thiếu ⇒ luôn trả `audit_ledger_unavailable`, KHÔNG BAO GIỜ phát
   lệnh.** Fail-closed nên an toàn, nhưng toàn bộ tính năng nằm im. **Đây là việc chưa xong.**
2. `remote_agent_pipeline.py:568` — thêm `"agent_not_in_lab_allowlist"` vào tuple lý do bỏ qua,
   nếu không mọi chẩn đoán ngoài allowlist đều ghi `AUTO_RECOVERY fail` gây nhiễu.

**Agent 5 tự nêu chỗ có thể sai (đáng đọc):**
- **Mắt xích LLM CHƯA được chứng minh.** Nó tự dựng `final` bằng tay để kích
  `dispatch_if_eligible`. **Chưa chứng minh vòng chẩn đoán sinh ra `suggested_recovery` cho
  một unit dừng thật** — mà agent 4 đo được 9/12 phiên có `suggested_recovery=null`.
  ⇒ **Đường ống đã thông; QUYẾT ĐỊNH dùng nó thì chưa.**
- Guard self-restart chưa có trên VM (chờ bản bundle sau) — hiện chỉ allowlist bảo vệ.
- Nó gate bằng **allowlist agent-id**, KHÔNG dùng `OMNI_ENV_MODE`, vì gateway báo `prod` còn
  worker báo `dev` — mâu thuẫn này **vẫn chưa giải quyết, là bẫy tiềm ẩn**.
- Đặt `omni:autonomous:terminal:{trace}` để re-planner K8s dừng ⇒ **coupling**: nếu ngữ nghĩa
  key đó đổi trong `autonomous_feedback_loop.py`, kết quả remote có thể kích re-plan K8s.
- `payment-api.service` là unit mô phỏng, restart luôn thành công. Diễn tập **chưa** kiểm ca
  mutation lỗi/chậm, redelivery, hay xung đột fencing.

### ✅ AGENT 2 ĐÃ XONG — quan sát LLM đã sống, và nó sửa 2 phép đo sai của tôi

**Tôi đã đo sai 2/3 điều lượt trước:**
- ❌ "Loki không nhận log omni" — SAI. Tôi tra label `app`; label thật là
  `namespace` / `pod_name` (`k8s/monitor/promtail.yaml:69`). Xác minh:
  `/loki/api/v1/labels` → `["filename","job","namespace","pod_name","stream"]`.
  **Lại đúng lớp lỗi "âm tính giả do query sai" mà tôi đã cảnh báo mọi agent khác.**
- ❌ "metric LLM là code chết" — SAI. `omni_llm_ttft_seconds` v.v. vẫn sống, đã có 68 lượt.
  Vấn đề thật: mọi series gộp vào `call_kind="unspecified"` nên nhãn vô nghĩa.
- ✅ "không có log per-call" — ĐÚNG, đó mới là khoảng trống thật.

**Điểm nối đúng: `src/llm/vllm_client.py`** — không nằm dưới `src/workers/` như tôi giả định
trong prompt. Đây là choke point duy nhất cho **28 call site** ⇒ instrument 1 chỗ thay vì 28.

File mới: `src/pkg/observability/llm_observability.py` · `k8s/monitor/dashboards/omni_llm.json` ·
`k8s/monitor/grafana-dashboard-llm.yaml` · `tests/test_llm_observability.py` (23) ·
`tests/test_llm_client_observability.py` (10). Sửa: `vllm_client.py`, `metrics_exporter.py`
(chỉ thêm), `src/pkg/rag/gate.py`.

Log `event=llm_call`: `trace, model, call_kind, outcome, duration_ms, prompt_chars,
response_chars, prompt_tokens, completion_tokens, prompt_sha, response_sha, endpoint`.
Metric: `omni_llm_calls_total{model,call_kind,outcome}`, `omni_llm_prompt_chars`,
`omni_llm_response_chars`, `omni_rag_gate_outcome_total{outcome,collection}`.

**INV_DATA_RESIDENCY xử lý đúng**: mặc định KHÔNG log nguyên văn — chỉ độ dài, token, và
SHA-256 12 ký tự. Preview là opt-in `OMNI_LLM_LOG_PREVIEW_CHARS` (mặc định 0, trần cứng 512),
có scrub secret. Chứng minh trong pod: prompt chứa `password=hunter2 host cust-db` chỉ ra
`prompt_chars=46 prompt_sha=9ac0e3a8d0f9`.

**Tôi xác minh độc lập**: 71 dòng `event=llm_call` · `omni_llm_calls_total{call_kind="chat"}
68.0` + `{structured} 3.0` (nhãn đã có nghĩa) · ConfigMap `grafana-dashboard-omni-llm` tồn tại ·
metric tới cả Prometheus **và Mimir** · span `llm.chat` nằm TRONG cùng trace với
`stage.EVIDENCE/RAG/CRAT/DISPATCH` trên Tempo. Suite **7222 passed**.

**Hai phát hiện instrumentation lộ ra ngay:**
1. 🔴 **`OMNI_RAG_GATE_ENABLED=false`** — tôi xác minh: env này được đặt TƯỜNG MINH trong
   deployment, trong khi `settings.py:823` mặc định là `True`. Gate trả `outcome=disabled`,
   **không tra tri thức lần nào**, nên mọi lượt trên đường đó đều đốt một lệnh gọi LLM thật.
   Cần quyết định có chủ đích, không phải để mặc.
2. **Prompt `structured` trung bình ~27.400 ký tự (~7k token)** vs `chat` 1.312, trong khi
   `OMNI_LLM_NUM_CTX=8192` ⇒ chạy **sát trần**. Nhiều khả năng nối với root-cause clip prompt
   đã ghi ở memory `advisory_prompt_clip_and_grounding_gate`.

**Việc còn treo — ĐÃ chuyển cho agent 3**: 63/67 lệnh gọi ghi `trace=-`. Nhánh
`remote_agent_pipeline` gọi LLM trong asyncio task **không kế thừa ContextVar** chứa trace id.
File đó thuộc vùng cấm của agent 2 ⇒ cần một dòng `push_trace_id` từ agent 3.

Ghi chú: agent 2 đổi giá trị nhãn `call_kind` từ `"unspecified"` sang `chat`/`structured` —
đổi *giá trị* nhãn, không thêm/xoá/đổi tên metric; không dashboard nào đang query nhãn đó.
`omni_llm_requests_total` vẫn 0 vì call site ở `advisory_analyst_handler.py` không hoạt động
trên deployment này — đã wired, không phải code chết.

### ✅ AGENT 1 ĐÃ XONG — 3 bản vẽ đã LÊN CẢ HAI MẶT
Route mới `/architecture`, 3 tab: Bản dễ hiểu (mặc định) · Sơ đồ kỹ thuật (6 Mermaid) ·
Ba câu hỏi bằng số đo.
- Lab `http://provider.ai-agent.local/architecture` · Public `https://app.omnisre.xyz/architecture`
- **Sửa giả định sai của tôi**: `ui/apps/provider-web` KHÔNG tồn tại. Next.js shell chính là
  `ui/apps/provider-portal` (build thành image `aoip-provider-web:latest`,
  `scripts/sync_public_plane.sh:79`); `aoip-provider-portal` là BFF FastAPI, không phải app Next.
- File mới: `ui/apps/provider-portal/app/architecture/{page,ArchitectureTabs,TechnicalView,PlainView,QuestionsView}.tsx`, `diagrams.ts`, `architecture.css`. Sửa `lib/nav.ts` + `ui-kit/src/Sidebar.tsx`.
- Nghiệm thu: `tsc --noEmit` sạch · `next build` OK · 6/6 Mermaid parse thật ra SVG qua
  Playwright · sync 2 mặt bằng `scripts/sync_public_plane.sh --ui --with-lab` (so imageID,
  không phải rollout restart tay) · lab+public đều 200 · payload 2 mặt **giống hệt** sau khi
  chuẩn hoá nonce CSP · `verify.sh` nhóm A (bất biến lab) PASS 3/3.
- **Tôi đã xác minh độc lập**: nav = 16 mục, `curl` lab `/architecture` → **200 / 99936 byte**
  (khớp chính xác con số agent báo).

**Tôi đã vá giúp cái nó bị chặn**: `tests/e2e_portals/specs/provider_overview.spec.ts:56`
`toHaveCount(14)` → `16` + thêm assertion "Bản vẽ kiến trúc". Assertion này **đã đỏ âm thầm
từ 2026-07-30** khi commit `9106660` thêm `/diagnostics` (→15) mà không ai cập nhật số.
Đã ghi chú ngay tại chỗ: nguồn sự thật là `lib/nav.ts`, đếm bằng `grep -c "href:"`.

**Còn tồn (agent 1 nêu, chưa xử):**
- Không chứng minh được render-sau-đăng-nhập trên **public**: `aoip-dex-public` chỉ có bcrypt
  hash và issuer là `https://app.omnisre.xyz/dex` nên OIDC không hoàn tất qua origin HTTP.
  Đổi issuer vi phạm INV_PUBLIC_PLANE_ISOLATED ⇒ đúng khi không đụng. Bằng chứng thay thế:
  pod public chạy **cùng imageID** với pod lab đã render đầy đủ trong trình duyệt thật.
- `verify.sh` 19 PASS / **1 FAIL** nhóm G — lỗi `origin-unreachable` đóng dấu 02:25Z, **~7 giờ
  trước** rollout; origin hiện trả 200 <60ms. Gate quét 400 dòng cuối nên lỗi cũ còn trong cửa sổ.
- Cố ý **không** dùng `make sync-public-all`: target đó rebuild `multi-agent-system:latest`
  trong khi agent khác đang sửa `src/` ⇒ sẽ nướng code dở dang vào image backend. Phán đoán đúng.

**Handoff này sẽ cũ ngay sau khi ghi** — agent còn đang sửa file. Phiên mới phải tự chạy
`git status --short` và `pytest --collect-only` để lấy trạng thái thật, đừng tin con số ở đây.
Không kết luận gì về working tree cho tới khi cả 5 agent báo xong.

### ⚠️ Sự cố 16:41 — cả 5 agent chết vì giới hạn phiên, đã nối lại
Cả 5 dừng đột ngột do **hạn mức phiên** (reset 14:20 giờ Sài Gòn), KHÔNG phải lỗi code.
Đã `SendMessage` nối lại cả 5 lúc 16:41, kèm mô tả trạng thái dở dang của từng agent.

**Hệ quả để lại — bộ test VỠ ở khâu thu thập:**
```
ModuleNotFoundError: No module named 'pkg.diagnostics.measurement_grounding'
7242/7253 tests collected, 1 error — Interrupted
```
Agent 3 chết đúng giữa nhịp TDD: đã viết `tests/test_diag_measurement_grounding.py`
(test đỏ) nhưng chưa kịp tạo `src/pkg/diagnostics/measurement_grounding.py`.
Đã yêu cầu nó vá cái này TRƯỚC MỌI VIỆC KHÁC vì nó chặn cả các agent song song.
**Nếu phiên mới thấy suite đỏ ở khâu collect — đây là lý do, không phải regression.**

Phần việc đã kịp hoàn thành trước khi chết (đã xác minh có file thật):
- `src/pkg/diagnostics/command_normalize.py` — 140 dòng (agent 3, sửa bug `ps`/`top`)
- `src/pkg/observability/llm_observability.py` — 346 dòng (agent 2)
- `src/llm/vllm_client.py` + metric `_llm_calls`/`_llm_prompt_chars`/`_llm_response_chars`/`_rag_gate_outcome` (agent 2)

**Phát hiện mạnh nhất tính tới lúc này** (agent 4, chưa hoàn tất):
> **0/809 trace remote chưa bao giờ chạm chặng `EXECUTOR`.**
Nghĩa là đường thực thi cho remote host nhiều khả năng **chưa từng chạy lần nào**, chứ
không phải "đang chạy nhưng bị gate chặn". Đã báo lại cho agent 5 để nó biết mình đang
MỞ đường mới chứ không phải sửa đường hỏng.

**Rủi ro cần chủ phiên xử lý:** agent 4 (chẩn đoán vì sao bị chặn) và agent 5 (mở chặn)
có thể kết luận ngược nhau. Đã dặn agent 4 nói thẳng nếu "không tự thực thi" là ĐÚNG
THIẾT KẾ, đừng uốn kết luận cho khớp việc của agent 5. Đây là việc chính của agent tổng hợp.

### ✅ AGENT 4 ĐÃ XONG — kết quả (4 agent còn lại vẫn chạy)

**`EXECUTOR` không nằm trên đường remote.** Remote host dùng stage `AUTO_RECOVERY`, không
phải `EXECUTOR`. Đo: `ra_with_EXECUTOR = 0/809`, `ra_with_AUTO_RECOVERY = 1/809` (và cái
đó `status=fail`). `EXECUTOR` chỉ có trên trace `gw-prom-*` (lane K8s). ⇒ [CỐ Ý], hai
substrate hai cơ chế. **Câu hỏi "vì sao EXECUTOR không chạy" đặt sai từ đầu — kể cả tôi.**

**Chặn thật: HTTP 423 tại `omni-gateway`.** Chuỗi gate (`src/gateway/routes/agent_runtime.py`):
`:225-232` `runtime_flag.aoip_mutation_enabled` → `:233-236` master kill-switch đọc env
**của gateway** → `:237`→`:202-215` `resolve_tier` → `tier_gate.py:112-113` → `:192-206`
plan ceiling.
Log: `event=auto_recovery_dispatched trace=ra-d645c49ed6d1 unit=aoip-agent.service http=423`

**Ba lỗi [SAI] agent 4 chỉ ra:**
1. **Kill-switch split-brain** — `agent_runtime.py:164-167` đọc env của **gateway**; worker
   có bản sao riêng. CLAUDE.md chỉ hướng dẫn sửa `omni-fullstack` ⇒ operator tin đã bật
   nhưng mọi mutate remote vẫn 423 câm. **Tôi đã mắc đúng lỗi này lượt trước.**
2. `auto_recovery_bridge.py:143` hardcode `"reason":"dispatched"` bất kể status ⇒ stage row
   `status=fail reason=dispatched` tự mâu thuẫn, mất sạch lý do 423.
3. Chữ "Omni không tự thực thi" hardcode ở `remote_diagnosis_emitter.py:191,201`; emit ở
   `remote_agent_pipeline.py:521` chạy **trước** dispatch `:523` nên thẻ không thể biết kết quả.

**Ba lỗi [TỆ]:** bỏ qua im lặng ở `remote_agent_pipeline.py:568-570` (808/809 trace không
ghi dòng nào) · **`omni:cfg:tier:default` là TÊN TENANT, không phải mặc định toàn cục** ·
`tenant_plan.autonomy_ceiling=assist` cho mọi tenant thật ⇒ risk MEDIUM vĩnh viễn HITL.

**Trần năng lực, không phải gate:** chỉ 3 capability
(`systemd.restart_unit|reset_failed|journal_vacuum`, `auto_recovery_bridge.py:33-35`).
9/12 session còn sống có `suggested_recovery=null` vì root cause là CPU/mem saturation —
**không capability nào chữa được**. Nên sự cố CPU tôi tiêm ở Đ5 **về nguyên tắc không bao
giờ kích hoạt được auto-recovery**, bất kể gate mở hay đóng.

### ⚠️ HAI DRIFT MỚI tôi đo lại sau báo cáo agent 4 (16:5x)
- **`omni-gateway` đang chạy `OMNI_ENV_MODE=prod`**, worker là `dev`. Gate nào keyed theo
  env_mode sẽ hành xử khác nhau giữa hai process. Nguy hiểm hơn drift #1 đã ghi trước đó.
- Giá trị runtime đang **biến động vì agent 5 sửa trực tiếp**: lúc agent 4 đo, gateway
  kill-switch = `false`; tôi đo lại = `true`. Redis nay có thêm `omni:cfg:tier:staging-sim`.
  PG `autonomy_tier_state`: `staging-sim=auto`, `default`/`tenant-replay-01`=`shadow`.
  **Không trích số runtime từ báo cáo agent 4 như sự thật hiện tại.**

### 🚨 CẢNH BÁO AN TOÀN đã chuyển cho agent 5
Ca duy nhất từng tới gateway đề xuất restart **`aoip-agent.service`** — chính agent của
Omni. Guard `AOIP_ALLOW_SELF_RESTART` ở `src/aoip/capabilities/systemd_restart.py:59`;
`run.env` trên `cust-edge` có `AOIP_ALLOWED_SYSTEMD_UNITS` (giá trị chưa đọc được, file
`0600`). Mở gate mà không kiểm = vòng lặp agent tự giết mình → mất telemetry toàn VM.

| # | Nhiệm vụ | Vùng file được giao (không chồng lấn) |
|---|---|---|
| 1 | 3 bản vẽ lên provider UI, lab + public | `ui/`, `k8s/ingress/` |
| 2 | Quan sát LLM: log/metric + dashboard Grafana | `src/pkg/`, client LLM (`src/llm/vllm_client.py`), `k8s/monitor/` |
| 3 | Nối grounding gate + sửa 3 bug lệnh | `remote_agent_pipeline`, `advisory_grounding_gate`, `os_diagnostic_loop`, `command_catalog`, command_executor |
| 4 | Vì sao `EXECUTOR` không chạy dù kill-switch bật | **CHỈ ĐỌC** — không sửa file nào |
| 5 | Remote agent thực thi thật kể cả mutate, trên 3 VM lab | `src/remote_agent/`, `src/aoip/agent/`, `pkg/autonomy/policy.py`, `autonomous_execute`, `gated_execute`, `k8s/deployments/`, cấu hình VM |

Cả 5 đều bị cấm `git commit`/`push`/tạo branch, và bị buộc chứng minh kết quả bằng
output lệnh thật trong pod/VM chứ không được tự nhận "xong".

**Agent thứ 6 — tổng hợp — CHƯA bật**, cố ý: nó cần kết quả của cả 5 làm đầu vào.
Việc của nó khi bật: đối chiếu chéo, tìm mâu thuẫn (đặc biệt **agent 4 chẩn đoán vì sao
bị chặn** vs **agent 5 mở chặn** — hai bên rất dễ kết luận ngược nhau), nối thành một
mạch nhân quả duy nhất, và soát xem bản vá của agent 3 có thật sự chặn được ca
`df -h → kết luận memory` hay không.

⚠️ Agent 5 sẽ còn nới thêm quyền trên VM. Khi nó báo xong, **bắt buộc đọc mục
"đã bật/nới gì kèm lệnh revert"** trong báo cáo của nó và gộp vào khối cảnh báo dưới đây.

## 🔴 QUYỀN MUTATE TRÊN VM ĐANG MỞ — ĐỌC TRƯỚC, ĐÂY LÀ MỤC QUAN TRỌNG NHẤT

Omni **đã tự sửa một dịch vụ thật trên VM khách** lúc 17:00:03 ngày 02/08 (diễn tập có
kiểm soát). Quyền đó **vẫn đang mở**. Đã có tiền lệ để quên kill-switch bật suốt 3 tuần —
xem post-mortem `drift-correction-2026-07-02.md`. **Không kết thúc giai đoạn đo mà không
quyết định giữ hay revert.**

### Trạng thái đã ĐO LẠI trực tiếp (không chép từ báo cáo agent)
```
omni-gateway  : OMNI_AUTO_EXECUTE_ENABLED=true   OMNI_ENV_MODE=prod   ← gate THẬT SỰ fire
omni-fullstack: OMNI_AUTO_EXECUTE_ENABLED=true   OMNI_ENV_MODE=dev
                OMNI_LAB_AUTO_EXECUTE_AGENTS=staging-sim_cust-app,_cust-edge,_cust-db
                OMNI_SIEM_SUGGEST_ONLY=false  OMNI_SHADOW_INFLUENCE_SUGGEST_ONLY=false
                OMNI_TELEGRAM_POLLING_ENABLED=true
PG runtime_flag       : staging-sim aoip_mutation_enabled=TRUE  (default/tenant-replay-01=false)
PG autonomy_tier_state: staging-sim tier=AUTO                   (default/tenant-replay-01=shadow)
Redis                 : omni:cfg:tier:default, omni:cfg:tier:staging-sim
```
⚠️ Kill-switch có **HAI nguồn độc lập** (gateway env vs worker env). CLAUDE.md chỉ hướng dẫn
sửa `omni-fullstack` ⇒ ai theo tài liệu sẽ tin đã tắt trong khi gateway vẫn mở. Đây là [SAI]
agent 4 chỉ ra và tôi đã mắc đúng lượt trước.

### REVERT — chạy đủ cả 5, thiếu một cái là vẫn mở
```bash
# 1. Công tắc một-nút để CHẶN mọi thực thi remote không người trực (mặc định code = rỗng)
kubectl set env deployment/omni-fullstack -n multi-agent OMNI_LAB_AUTO_EXECUTE_AGENTS-

# 2. Kill-switch GATEWAY — đây mới là cái gate thật sự chặn
kubectl set env deployment/omni-gateway -n multi-agent OMNI_AUTO_EXECUTE_ENABLED=false

# 3. Kill-switch WORKER (bản sao riêng, phải làm cả hai)
kubectl set env deployment/omni-fullstack -n multi-agent \
  OMNI_AUTO_EXECUTE_ENABLED=false OMNI_SIEM_SUGGEST_ONLY=true \
  OMNI_SHADOW_INFLUENCE_SUGGEST_ONLY=true

# 4. Cờ mutate theo tenant (trước là false, version 12)
kubectl exec -n multi-agent omni-postgres-0 -- psql -U omni -d omnidb -c \
  "update omni_admin.runtime_flag set flag_value='false', updated_by='revert', version=version+1 \
   where tenant_id='staging-sim' and flag_key='aoip_mutation_enabled';"

# 5. Tier — PG *và* Redis cache, bắt buộc cả hai
kubectl exec -n multi-agent omni-postgres-0 -- psql -U omni -d omnidb -c \
  "update omni_admin.autonomy_tier_state set tier='shadow', updated_by='revert', version=version+1 \
   where tenant_id='staging-sim';"
kubectl exec -n multi-agent redis-0 -- redis-cli DEL omni:cfg:tier:staging-sim omni:cfg:tier:default
```

### Guard tự-giết: ĐÃ XÁC MINH AN TOÀN (tôi tự đo trên cả 3 VM)
```
AOIP_ALLOWED_SYSTEMD_UNITS=payment-api.service,systemd-journald.service
```
`aoip-agent.service` **KHÔNG** nằm trong danh sách ⇒ nguy cơ agent tự restart chính nó
hiện bị chặn bởi allowlist. Lưu ý agent 5 nói rõ: guard `AOIP_ALLOW_SELF_RESTART` mới thêm
vào code **chưa có trên VM** (đi kèm bản bundle sau; nó cố ý không chép tay để khỏi phá
`bundle_hash`). Nên hiện tại **allowlist là lớp bảo vệ DUY NHẤT** — đừng thêm
`aoip-agent.service` vào đó.

## Deliverable
Phản biện hệ thống bằng số đo, rồi thực thi Đ1–Đ5.

## Đã xong

| # | Việc | Bằng chứng |
|---|---|---|
| Đ1 | Gauge KPI không còn biến "chưa biết" thành 0.0 | Sau vá, `omni_kpi_advisory_acceptance_rate` **0 series** (trước: `0.0`). 4 alert rule thêm `and omni_kpi_advisory_total > 0`; thêm `OmniKpiNoSamples` |
| Đ2 | Xoá 4 bản ghi KPI test 83 ngày tuổi | `omni:kpi:*` rỗng. Sao lưu: scratchpad `backup-test-data-2026-08-02.txt` |
| Đ3 | Xoá 3 hàng `playbook_graduation` test | `count(*)=0`. Điều kiện xoá: `track='advisory' and crat_ref is null and updated_by='system'` |
| Đ4 | `get_summary` per-tenant, gauge có nhãn `tenant` | `test_kpi_no_data_not_zero.py` 9 test |
| Đ5 | Chạy trọn luồng bằng lỗi THẬT | Tải 8 nhân CPU trên `cust-app` → `promoted ANOMALY by=omni_baseline z=4.316 value=98.9 confidence=78/AUTONOMOUS` → RAG 0.984 → urgency=critical → **3 thẻ Telegram đã tới** |

File đổi: `src/workers/metrics_exporter.py` · `src/workers/kpi_metrics.py` ·
`k8s/monitor/prometheus.yaml` · `tests/test_kpi_no_data_not_zero.py` (mới) ·
`docs/architecture/SYSTEM_DIAGRAMS.md` (lượt trước).
Test: **7189 pass** + 9 test mới. Đã build+deploy, đã `hasattr()` xác minh trong pod.

## ⛔ Phát hiện lớn từ Đ5 — chất lượng chẩn đoán, CHƯA SỬA

Ba thẻ, ground truth do chính tôi tạo (chỉ nạp CPU lên `cust-app`, không đụng máy khác):

| Trace | Kết luận của Omni | Thực tế |
|---|---|---|
| `#689e6dc5` cust-app | "Insufficient memory (mem 60%)" | **SAI**. 60% RAM là bình thường và **giống hệt trên cả 3 VM**, kể cả máy rảnh |
| `#d645c49e` cust-edge | "aoip-agent.service crashing" | **SAI**. `systemctl is-failed` rc=1 = *không có unit hỏng*; journalctl trưng log **13/07**, cách 3 tuần. Tôi chưa từng nạp tải lên cust-edge |
| `#da66cac8` cust-app | "CPU saturation, load_avg 11.89" | **ĐÚNG** — nhưng **cả 2 lệnh điều tra đều fail**, không có bằng chứng nào |

**Chuỗi hỏng của `#689e6dc5`** (đọc từ `omni:diag:session:ra-689e6dc59ea4`):
- Turn 1: giả thuyết ĐÚNG "CPU saturation", `confidence=0.75`,
  `evidence_gaps=["No information about disk usage or memory pressure"]` →
  xin chạy **`df -h`** (lệnh ĐĨA) để điều tra sự cố CPU.
- Turn 2: `df` trả về khoẻ (18%) ⇒ LLM suy "đĩa ổn ⇒ chắc do bộ nhớ", **bỏ giả thuyết đúng**,
  đổi sang "Insufficient memory", `confidence 0.75 → **0.95**`, `evidence_gaps=[]`,
  `diagnosis_complete_claimed=true` — **chưa từng đo bộ nhớ một lần nào**.

Nguyên nhân gốc: **`apply_advisory_grounding_gate` KHÔNG được nối vào vòng chẩn đoán
remote-agent.** Nó chỉ có ở `advisory_analyst_handler.py:422`. Grep
`remote_agent_pipeline.py` / `os_diagnostic_loop.py` → **rỗng**. Đường sinh ra 3 thẻ này
không có cổng chống bịa nào.

**Bug lệnh (2 cái, cụ thể, dễ sửa):**
1. `ps` nhận `args=["aux --sort=-%cpu"]` — cả chuỗi nhồi vào MỘT phần tử ⇒
   `error: unsupported option (BSD syntax)`. Cần tách khoảng trắng ở biên (không tin
   hình dạng output của LLM).
2. `top` gọi không cờ ⇒ fail ngoài tty. Phải là `top -b -n 1`.
3. `exit_code` lưu `None` trong session dù thẻ hiển thị `rc=1` — hai đầu không khớp.

## ⛔ Quan sát LLM — KHÔNG TỒN TẠI
`grep llm_request|llm_response|prompt=` trong log 20 phút → **0 dòng**. Loki không có
label `app` nào. Muốn biết LLM nhận/trả gì hiện phải đọc tay
`omni:diag:session:{trace}` trong Redis. LGTM+ stack đang chạy đủ 9 pod (grafana, loki,
mimir, tempo, promtail, prometheus, alertmanager, kube-state-metrics, node-exporter)
nhưng **không có dashboard/trace nào cho LLM**.

## Việc CÒN LẠI (user đã yêu cầu, chưa làm)
1. **Đưa 3 bản vẽ lên provider UI** — cả lab (`provider.ai-agent.local`) và public
   (`app.omnisre.xyz`). Bắt buộc dùng `make sync-public-ui`, KHÔNG `kubectl rollout restart`.
2. **Cập nhật monitor cho LLM**: log/trace prompt+response, dashboard Grafana.
3. Sửa 3 bug lệnh + nối grounding gate vào vòng chẩn đoán remote-agent.
4. Vì sao `EXECUTOR` vẫn không chạy dù `auto_execute=true` (thẻ vẫn ghi "Omni không tự
   thực thi") — chưa điều tra.

## Không được làm lại
- **Không dùng `$VAR` chứa lệnh rồi mong zsh tách từ** — đã dính lần nữa lượt này, cho
  ra 7 dòng "0 key" hoàn toàn giả.
- **Không tin "rollout successful" là đã có code mới** — luôn `hasattr()` trong pod.
- **Không vá một Deployment rồi tưởng metric đã sạch**: `omni-onboarding` xuất cùng tên
  gauge, phải restart nó thì alert mới tắt.
- **Không coi 3 hàng `playbook_graduation` là bằng chứng vòng học chạy thật** — cả 3 đều
  `crat_ref=null`, `updated_by=system`: fixture của phiên G1.

## Task list
- **#1–#5** [completed] Đ1 · Đ2 · Đ3 · Đ4 · Đ5
- **#6** [pending] 3 bản vẽ lên provider UI (lab + public)
- **#7** [pending] Monitor LLM: log/trace prompt+response + dashboard
- **#8** [pending] Grounding gate cho vòng chẩn đoán remote-agent
- **#9** [pending] Sửa `ps` args splitting, `top -b -n 1`, `exit_code=None`
- **#10** [pending] Quyết định giữ hay revert kill-switch
- **#11** [pending] Commit (chưa commit gì lượt này)

## Tài liệu liên quan
- `docs/architecture/SYSTEM_DIAGRAMS.md` — 6 sơ đồ verified
- `src/workers/advisory_grounding_gate.py` — cổng chống bịa, đang thiếu call site
- `omni:diag:session:{trace}` trong Redis — nơi duy nhất thấy được LLM nghĩ gì
