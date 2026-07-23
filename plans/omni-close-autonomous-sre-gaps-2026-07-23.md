# Plan: Đóng 4 gap Autonomous SRE Vision ↔ Code thật (2026-07-23)

> Direct-edit mode (không branch/PR — repo dùng trunk-based, xem CLAUDE.md AUTONOMY RULES).
> Baseline: HEAD `bae1781` (docs consolidation + SIEM audit follow-up, đã commit trước khi bắt đầu).

## Mục tiêu

Agent hành xử như 1 Senior SRE mới nhận bàn giao hệ thống khách: discover → hỏi khi
không biết → verify khi biết → thực thi action có giám sát → báo cáo. Đóng 4 gap cụ
thể giữa vision và code, KHÔNG rebuild, KHÔNG mở lại design đã freeze
(`docs/architecture/FRAMEWORK_LAWS.md` — CONSTITUTIONAL).

## Baseline đã kiểm chứng (KHÔNG audit lại)

- Discovery read-only thật: `src/remote_agent/discovery.py`
- Ask-human thật: `src/aoip/question_lifecycle.py` (Unknown→Question→Answer→Claim, Telegram)
- Execution có gate thật: `src/aoip/capabilities/systemd_restart.py` (observe_only default, human-approved)
- **Action library đã có 3 capability mutation** (không phải 1 như audit cũ giả định —
  đã xác minh lại qua `git log --diff-filter=A`): `systemd_restart` (2026-07-01),
  `systemd_reset_failed` (2026-07-21), `systemd_journal_vacuum` (2026-07-21). Cộng
  4 capability read-only/orchestration: `inspect_host`, `understand_host`,
  `map_system_graph`, `missions`, và 1 K8s: `restart_deployment`.

## Thứ tự phase đã chốt (KHÔNG đổi): 3 → 4 → 2 → 1 → 5

Lý do: rủi ro thấp trước (điều tra path trùng), rồi kiến trúc (RAG grounding, confidence
boundary), rồi năng lực (action library), rồi tiêu chí nghiệm thu E2E cuối.

## KHÔNG ĐƯỢC LÀM (áp dụng toàn bộ 5 phase)

- Không mở lại design đã freeze (Vision/Laws/Meta/Semantic/Capability/Org/Knowledge/
  Learning/Execution models) trừ khi phase chứng minh bắt buộc — phải ghi rõ lý do.
- Không tự ý commit/push git — hỏi trước mỗi lần.
- Không đổi `AOIP_AGENT_MODE` default sang mutation-enabled trên host thật nào mà
  không có xác nhận trực tiếp của user.
- Không audit lại 18-domain hay `docs/` từ đầu.
- Không thêm primitive verb thứ 9 (`INV_MINIMAL_PRIMITIVES`) — mọi capability mới
  phải là composition của 8 verb khoá: Validate/Plan/Execute/Observe/Verify/Recover/
  Escalate/Abort qua 5 toán tử {Sequence, Choice, Loop, Parallel, Interrupt}.

## Quy trình mỗi phase

`/plan` → tdd (test trước, `FakeRedis(decode_responses=True)` cho ZSET, `asyncio_mode=auto`)
→ code → `/code-review` → `/verify` → cập nhật `docs/handoffs/CURRENT_SESSION.md` (≤20 dòng).

---

## Phase 1 (thực thi thứ 3 trong chuỗi) — Xác nhận + chính thức hoá ranh giới "hỏi khi không biết"

**Model**: Sonnet.

**PREMISE ĐÃ SỬA sau adversarial review (đừng lặp lại giả định ban đầu)**: review đối
kháng (Opus) đã đọc code thật và xác nhận `question_lifecycle.py:3-10` ĐÃ ghi rõ
`_detect_gaps_and_ask` (legacy, `workers/onboarding_pipeline.py`) được giữ nguyên có
chủ đích ("Bước 7 compatibility"). Hai path khác nhau về bản chất, không phải trùng
lặp: onboarding hỏi free-text theo probe (service_topology/port_scan/api_access,
`onboarding_pipeline.py:177-219`, ghi `dd.QUESTIONS_KEY`, feed readiness/UI) —
`question_lifecycle.py` là entity/facet-aware (owner/monitoring/sla, dedup
fingerprint, Claim). Overlap thực tế thấp. **Mặc định của phase này là XÁC NHẬN +
CHÍNH THỨC HOÁ ranh giới, KHÔNG migrate-and-delete.** Nhánh xoá path cũ chỉ được chọn
nếu điều tra thực tế (bước 3 dưới) tìm thấy bằng chứng ngược lại rõ ràng — không phải
lựa chọn ngang hàng như bản nháp đầu.

**Dependency lưu ý (H2 từ review)**: `question_lifecycle.py:27` import
`FACET_PREDICATE, EntityCompetency, FacetState` từ `competency_matrix.py` — file này
là input của Phase 3. Vì Phase 3 chạy TRƯỚC Phase 1 trong chuỗi 3→4→2→1→5, Phase 1
executor phải re-read `competency_matrix.py` sau khi Phase 3 xong để nắm import có
đổi field/shape không, trước khi viết docstring ranh giới.

**Việc cần làm**:
1. Đọc lại `question_lifecycle.py:3-10` (docstring đã có) — xác nhận nội dung khớp
   với review trên.
2. Đọc đầy đủ `_detect_gaps_and_ask` trong `workers/onboarding_pipeline.py` (177-219)
   — input/output/consumer thật (readiness/UI).
3. Nếu xác nhận 2 path phục vụ mục đích khác nhau (kỳ vọng): viết rõ ranh giới thành
   văn bản chính thức trong docstring cả 2 file + 1 mục ngắn trong
   `docs/architecture/` — giải thích tại sao đây KHÔNG vi phạm
   `INV_SINGLE_SOURCE_OF_TRUTH` (2 loại câu hỏi khác nhau, không phải 2 nguồn sự thật
   cho cùng 1 câu hỏi).
4. CHỈ khi bước 1-2 lộ ra overlap thật (ví dụ cả 2 cùng ghi 1 loại gap cho cùng 1
   consumer) mới xét migrate — nếu vậy, viết test trước (TDD) xác nhận
   `onboarding_pipeline` gọi đúng `question_lifecycle`, không phá test/consumer hiện có
   (đặc biệt readiness endpoint).

**File liên quan**: `src/aoip/question_lifecycle.py`, `src/workers/onboarding_pipeline.py`,
`src/aoip/competency_matrix.py` (chỉ đọc, không sửa ở phase này — Phase 3 sở hữu),
`tests/` tương ứng.

**Exit criteria**:
- Văn bản ranh giới chính thức tồn tại trong cả 2 docstring + `docs/architecture/`
  (trường hợp mặc định), HOẶC bằng chứng overlap thật + migrate hoàn tất (trường hợp
  ngoại lệ, phải nêu lý do cụ thể tại sao khác với kết luận review).
- `grep -rn "_detect_gaps_and_ask" src/` vẫn còn hit (path giữ nguyên) trừ khi rơi vào
  trường hợp ngoại lệ migrate.
- `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` xanh, không giảm
  test count so với trước phase.
- Cập nhật `docs/handoffs/CURRENT_SESSION.md`.

**Rollback**: phase này mặc định chỉ viết docstring/docs (an toàn, dễ revert). Nếu rơi
vào nhánh migrate, `git diff` chỉ đụng 2-3 file — revert bằng `git checkout --` nếu
quyết định sai.

---

## Phase 2 (thực thi thứ 4 trong chuỗi) — Nối tài liệu nghiệp vụ vào reasoning (gap 4)

**Model**: Opus (yêu cầu reasoning kiến trúc — grounding gate).

**Context brief**: `ingest_customer_knowledge()` (`src/services/knowledge/
document_store.py`) lưu tài liệu nghiệp vụ khách hàng (metadata + summary ≤2000 chars,
`INV_DATA_RESIDENCY`) nhưng KHÔNG có consumer nào trong `src/services/analyst/` hoặc
reasoning loop đọc lại trước khi LLM ra advisory. Đây là gap trực tiếp nhất tới
"vận hành dựa trên tài liệu".

**Việc cần làm**:
1. Đọc `document_store.py::ingest_customer_knowledge` — schema lưu trữ thật (Redis
   key, TTL, format).
2. Đọc RAG retrieval hiện có (`src/rag/`) — cách `omni-analyst` gọi RAG trước LLM
   advisory reasoning (đã có pattern tương tự cho SOP — tái dùng, không viết lại).
3. Thiết kế: thêm bước retrieval customer-knowledge (metadata/summary only, KHÔNG kéo
   full content — `INV_DATA_RESIDENCY`) vào prompt trước khi gọi LLM, tương tự cách
   RAG SOP đã grounding (xem memory `project_advisory_prompt_clip_and_grounding_gate`:
   gate phải case-insensitive, clip 26% root-cause).
4. TDD: test trước — advisory reasoning với customer-knowledge injected vs không, xác
   nhận content không vượt 2000 chars, xác nhận full document KHÔNG bị kéo vào prompt.
5. **(M1 từ review)** Tài liệu khách hàng KHÔNG tự động thành Fact đã verify — context
   inject vào prompt phải đánh dấu rõ nguồn "customer-provided, chưa verify" (tương tự
   cách RAG SOP phân biệt recalled-answer vs LLM-generated), tuân
   `INV_LLM_NOT_FIRST`/verify-before-believe: advisory không được coi tài liệu khách
   là bằng chứng đã kiểm chứng ngang hàng với probe thật.
6. Benchmark: chạy lại advisory benchmark (`make benchmark-advisory`) để xác nhận
   không regression so với baseline hiện tại.

**File liên quan**: `src/services/knowledge/document_store.py`,
`src/services/analyst/` (reasoning loop), `src/rag/`, test tương ứng.

**Exit criteria**:
- Consumer thật gọi `ingest_customer_knowledge` output trước LLM advisory reasoning
  (grep xác nhận, không phải chỉ thiết kế trên giấy).
- Test TDD xanh, xác nhận `INV_DATA_RESIDENCY` giữ nguyên (không leak full content).
- `make benchmark-advisory` không tụt điểm so với số đo trước phase.
- Cập nhật `docs/handoffs/CURRENT_SESSION.md`.

**Rollback**: thay đổi khu trú trong reasoning loop — nếu benchmark tụt điểm, revert
bước inject, giữ nguyên `ingest_customer_knowledge` (không đụng, đã hoạt động đúng).

---

## Phase 3 (thực thi thứ 2 trong chuỗi) — Hợp nhất/định ranh giới 2 hệ confidence (gap 2)

**Model**: Opus (yêu cầu reasoning kiến trúc — invariant `INV_SINGLE_SOURCE_OF_TRUTH`).

**Context brief**: `VerificationResult` (`src/aoip/verification.py`) và `FacetState`
(`src/aoip/competency_matrix.py`) là 2 hệ confidence song song. Cần xác định: có thực
sự đo cùng 1 thứ (vi phạm SSOT) hay đo 2 chiều khác nhau (per-action vs
per-knowledge-facet — hợp lệ, chỉ cần viết rõ contract).

**PREMISE ĐÃ SỬA sau adversarial review (H3)**: review đối kháng đã đọc code thật —
`competency_matrix.py:5-9` ghi rõ `FacetState` là DERIVED PROJECTION, không persist
(`INV_DERIVED_NEVER_PERSIST`); `VerificationResult` là contract per-mutation
transient. Hai trục có bản chất khác nhau, chưa thấy bằng chứng cùng 1 truth persist
song song → **mặc định phase này nghiêng về "giữ ranh giới rõ", KHÔNG hợp nhất**,
trừ khi bảng field-by-field (bước 3) lộ ra bằng chứng ngược lại cụ thể. **CẢNH BÁO**:
nếu chọn nhánh hợp nhất, TUYỆT ĐỐI không persist `FacetState` để làm truth chung —
đó là vi phạm `INV_DERIVED_NEVER_PERSIST` trực tiếp, không phải chi tiết implementation
có thể xuê xoa.

**Downstream lưu ý (C1 từ review)**: `systemd_reset_failed.py:368` (một trong 3
capability hiện có, và là file mẫu cho Phase 4) gọi `outcome.verification.to_dict()`
trực tiếp trên `VerificationResult`. Nếu phase này đổi shape/field của
`VerificationResult`, MỌI capability ở Phase 4 (chạy ngay sau, thứ tự 3→4) sẽ vỡ.
Phase 4 executor bắt buộc phải re-read `verification.py` sau khi Phase 3 xong trước
khi viết capability mới — không dùng hiểu biết cũ về shape của `VerificationResult`.

**Việc cần làm**:
1. Đọc đầy đủ `verification.py` — `VerificationResult` áp dụng khi nào, ai tạo, ai đọc.
   Đặc biệt: liệt kê MỌI consumer hiện tại (bao gồm `systemd_reset_failed.py`,
   `systemd_journal_vacuum.py`, `systemd_restart.py`) để biết breaking-change surface.
2. Đọc đầy đủ `competency_matrix.py` — `FacetState` áp dụng khi nào, ai tạo, ai đọc.
3. Vẽ bảng: field-by-field, cái nào trùng ý nghĩa (không chỉ trùng tên).
4. Quyết định (mặc định nghiêng "giữ ranh giới" — xem premise ở trên):
   - Nếu bảng field-by-field lộ bằng chứng thật là cùng 1 khái niệm confidence bị
     duplicate → hợp nhất, nhưng KHÔNG BAO GIỜ persist `FacetState` riêng — nó luôn
     derive tại thời điểm đọc.
   - Mặc định: viết contract rõ ràng trong docstring cả 2 file + 1 đoạn trong
     `docs/architecture/` giải thích ranh giới (per-action vs per-facet), không hợp
     nhất.
5. Nếu hợp nhất: TDD cho phần code thay đổi, VÀ chạy lại toàn bộ test của 3 capability
   hiện có (`systemd_restart`, `systemd_reset_failed`, `systemd_journal_vacuum`) để
   xác nhận không vỡ contract `VerificationResult`.

**File liên quan**: `src/aoip/verification.py`, `src/aoip/competency_matrix.py`,
(chỉ đọc để kiểm tra breaking-change) `src/aoip/capabilities/systemd_*.py`.

**Exit criteria (đã cụ thể hoá — M2)**:
- 1 quyết định bằng văn bản (hợp nhất HOẶC ranh giới rõ), có bảng field-by-field làm
  bằng chứng, đính kèm trong `docs/architecture/`.
- Nếu hợp nhất: `grep -n "persist\|save\|write" ` trên code xử lý `FacetState` xác
  nhận không có write-path mới cho derived data; `.venv/bin/python -m pytest
  tests/test_verification*.py tests/test_competency*.py -q` xanh; 3 capability hiện
  có (`test_systemd_*.py`) vẫn pass không sửa assertion.
- Nếu giữ ranh giới: docstring 2 file trích dẫn lẫn nhau (cross-reference), đoạn
  `docs/architecture/` nêu rõ 2 câu hỏi cụ thể "X thuộc VerificationResult hay
  FacetState?" với ví dụ cụ thể — không phải mô tả chung chung.
- Cập nhật `docs/handoffs/CURRENT_SESSION.md`.

**Rollback**: nếu hợp nhất sai (mất field cần thiết ở consumer nào đó, hoặc vỡ 1 trong
3 capability hiện có), revert qua git, giữ nguyên 2 object tách biệt với ranh giới ghi
rõ thay thế.

---

## Phase 4 (thực thi thứ 1 trong chuỗi) — Mở rộng action library, domain remote-host/VM (gap 1)

**Model**: Sonnet.

**Context brief**: Action library ĐÃ CÓ 3 capability mutation (`systemd_restart`,
`systemd_reset_failed`, `systemd_journal_vacuum`) — đây là nền, KHÔNG xây từ 0. Cần
thêm 3-5 capability mới, domain remote-host/VM (khớp vision "chân tay vận hành hệ
thống khách hàng", tận dụng 3 VM lab đã có sẵn để test thật: `cust-edge`, `cust-app`,
`cust-db`). Mỗi capability PHẢI là composition của 8 primitive verb khoá qua 5 toán
tử — KHÔNG thêm verb mới (`INV_MINIMAL_PRIMITIVES`).

**BẮT BUỘC trước khi viết code (C1 từ review)**: Phase 3 chạy ngay trước phase này
(thứ tự 3→4) và có thể đổi shape/field của `VerificationResult`
(`src/aoip/verification.py`), thứ mà `systemd_reset_failed.py:368` — file mẫu cấu
trúc cho phase này — tiêu thụ trực tiếp qua `outcome.verification.to_dict()`. **Đọc
lại `verification.py` VÀ kết quả quyết định của Phase 3 (docstring/docs mới) TRƯỚC
khi viết capability mới** — không dùng hiểu biết cũ về shape của `VerificationResult`
từ trước khi Phase 3 chạy. Phase này KHÔNG được chạy song song với Phase 3 dù về mặt
file không đụng nhau trực tiếp — đây là dependency qua contract, không phải qua file.

**Việc cần làm**:
1. Đọc 1 capability hiện có làm mẫu cấu trúc (khuyến nghị `systemd_reset_failed.py` —
   mới nhất, chuẩn nhất theo pattern hiện tại: `CapabilityRejected`, `_EvidenceCtx`,
   `validate_unit_name`, allowlist unit chung) — đọc SAU khi Phase 3 đã chốt quyết
   định, không đọc trước rồi giả định không đổi.
2. Chọn 3-5 capability mới phù hợp domain remote-host/VM — ví dụ (xác nhận lại với
   thực tế 3 VM lab trước khi chốt danh sách cuối): disk cleanup có gate (tương tự
   journal_vacuum nhưng target khác), process kill theo allowlist, config file
   rollback-from-backup, package version pin/rollback qua `command_executor.py`
   allowlist (đã hardening trong audit follow-up vừa commit). **Đã loại "network
   interface reset" khỏi danh sách (H4 từ review — agent tự cắt kết nối chính mình
   qua remote host, không rollback được, vi phạm blast-radius nhỏ).** Mỗi capability
   PHẢI khai báo rõ reversibility (theo đúng pattern `systemd_reset_failed` đã làm) —
   nếu không chứng minh được rollback path, không đưa vào danh sách.
3. Mỗi capability: TDD trước — `CapabilityRejected` path, allowlist validate, evidence
   context, mặc định `observe_only`/`shadow`, human-approved trước mutate thật.
4. Test thật trên ít nhất 1 VM lab (không chỉ unit test mock) cho 1-2 capability đại
   diện, theo đúng `feedback_chaos_test_protocol` (no happy path, assert FAIL=bug
   confirmed).

**File liên quan**: `src/aoip/capabilities/` (file mới), `src/remote_agent/
command_executor.py` (nếu tái dùng allowlist), test tương ứng.

**Exit criteria**:
- 3-5 capability mới, mỗi cái pass TDD (Validate reject path + Execute + Verify +
  Recover nếu áp dụng).
- Ít nhất 1 capability verify trên VM lab thật (không chỉ mock).
- `MUTATE_TOOL_ALLOWLIST` cập nhật đúng, RBAC không mở rộng ngoài phạm vi cần thiết
  (theo invariant RBAC trong CLAUDE.md).
- Cập nhật `docs/handoffs/CURRENT_SESSION.md`.

**Rollback**: mỗi capability là 1 file độc lập — xoá file + entry allowlist nếu cần
revert, không ảnh hưởng capability khác.

---

## Phase 5 — E2E: kịch bản "Senior SRE nhận bàn giao hệ thống mới" (tiêu chí nghiệm thu)

**Model**: Sonnet.

**Context brief**: Đây là tiêu chí nghiệm thu cho TOÀN BỘ vòng lặp 5 phase — không
phải build thêm capability, mà là chứng minh 4 gap đã đóng thực sự nối được với
nhau thành 1 luồng hành vi thật.

**Kịch bản** (chạy trên VM lab thật, không mock toàn bộ):
1. Discover: agent chạy discovery read-only trên 1 VM lab chưa từng thấy (hoặc coi
   như mới bằng cách xoá cache/snapshot).
2. Hỏi người: khi gặp gap kiến thức (ví dụ: unit systemd lạ không có trong KB), agent
   tạo Question qua `question_lifecycle.py` (ranh giới đã chính thức hoá ở Phase 1),
   gửi Telegram.
3. Nhận trả lời: user (hoặc script giả lập trả lời) trả lời qua Telegram, Answer →
   Claim.
4. Verify: agent verify Claim bằng probe thật (không chỉ tin lời khai).
5. Thực thi: agent chọn 1 trong các capability mới ở Phase 4, qua đúng gate
   observe_only → human-approved → execute thật trên VM lab.
6. Báo cáo: agent tổng hợp toàn bộ luồng thành 1 báo cáo (tận dụng
   `unified_incident_card.py` pattern nếu phù hợp, hoặc format tương đương).

**Việc cần làm**:
1. Viết test E2E kịch bản trên (không phải unit test rời rạc) — thật trên VM lab,
   theo `remote-agent-test` skill convention (E2E-first, không mock nội bộ).
2. Chạy, ghi lại kết quả từng bước (pass/fail + evidence).
3. Nếu fail ở bước nào → xác định gap đó thuộc phase nào trong 1-4, quay lại sửa
   (không patch tạm trong Phase 5).

**File liên quan**: `tests/e2e_*` (thư mục tương ứng), có thể cần script giả lập trả
lời Telegram cho CI (không thể chờ người thật trong test tự động).

**Exit criteria**:
- Kịch bản chạy hết 6 bước trên VM lab thật, PASS.
- Báo cáo cuối liệt kê rõ: gap nào đã đóng thật (có bằng chứng), gap nào còn hở
  (không được giấu, theo `feedback_chaos_test_protocol`).
- Cập nhật `docs/handoffs/CURRENT_SESSION.md` với kết quả E2E cuối cùng.

**Rollback**: N/A (đây là phase xác nhận, không mutate code trừ khi phát hiện bug
cần sửa từ phase trước — sửa thì quay lại đúng phase gốc).

---

## Dependency graph (SỬA sau adversarial review — C1)

**QUAN TRỌNG**: bản nháp đầu claim "4 phase độc lập file, có thể chạy song song" —
adversarial review (Opus) đã bác bỏ claim này bằng bằng chứng code thật:

- Phase 3 (`verification.py`) → Phase 4 (`systemd_reset_failed.py:368` tiêu thụ
  `VerificationResult.to_dict()` trực tiếp): **dependency qua CONTRACT, không qua
  file**. Đổi shape ở Phase 3 mà không re-check sẽ vỡ Phase 4.
- Phase 3 (`competency_matrix.py`) → Phase 1 (`question_lifecycle.py:27` import
  `FACET_PREDICATE, EntityCompetency, FacetState` từ file đó): dependency qua IMPORT.

```
Phase 3 (confidence boundary) ──┬──[contract VerificationResult]──> Phase 4 (action library)
                                 └──[import FacetState]────────────> Phase 1 (question boundary)

Phase 2 (RAG grounding) ─────────────────────────────────────────────────────┐
Phase 4 (action library) ────────────────────────────────────────────────────┤──> Phase 5 (E2E)
Phase 1 (question boundary) ─────────────────────────────────────────────────┘
```

**KHÔNG được chạy Phase 3 song song với Phase 1 hoặc Phase 4** dù không đụng chung
file — đây chính là lý do thứ tự tuần tự 3→4→2→1→5 mà user đã chốt là ĐÚNG, không
phải tuỳ chọn để tối ưu tốc độ. Phase 2 (RAG grounding, `document_store.py`/`rag/`)
là phase duy nhất thực sự độc lập cả file lẫn contract — nhưng vẫn giữ nguyên vị trí
thứ 3 trong chuỗi theo quyết định của user. Phase 5 PHẢI chạy sau cả 4 phase kia.

## Orchestration (theo yêu cầu user — 8 vai trò qua Workflow)

Thứ tự chạy PHẢI tuần tự đúng 3→4→2→1→5 (không song song — xem dependency graph ở
trên); "8 vai trò" nghĩa là 8 agent riêng biệt, không phải 8 luồng chạy đồng thời.

1. Blueprint (agent này — đã hoàn thành, tạo + sửa file này sau review).
2. Phase 3 executor (Opus) — chạy trước, không ai được chạy song song với nó.
3. Phase 4 executor (Sonnet) — chạy sau khi (2) xong, PHẢI re-read
   `verification.py` + quyết định của Phase 3 trước khi viết code.
4. Phase 2 executor (Opus) — độc lập, có thể chạy song song với (2)/(3) nếu muốn tối
   ưu, nhưng theo quyết định user vẫn giữ vị trí thứ 3 trong chuỗi tuần tự.
5. Phase 1 executor (Sonnet) — chạy sau khi (2) xong, PHẢI re-read
   `competency_matrix.py` trước khi viết docstring ranh giới.
6. Phase 5 executor (Sonnet) — chạy cuối cùng, sau khi (2)(3)(4)(5) đều xong.
7. Giám sát/phản biện — theo dõi cả 5 phase executor theo thời gian thực (không chỉ
   hậu kỳ), có quyền YÊU CẦU executor dừng lại và báo cáo lên vai trò tổng hợp nếu
   phát hiện vi phạm KHÔNG ĐƯỢC LÀM hoặc lệch premise đã sửa ở trên (ví dụ: Phase 1
   executor tự ý chọn migrate-and-delete mà không có bằng chứng overlap mới); **giới
   hạn thật của vai trò này**: nó không có quyền tự sửa code hay chặn commit — chỉ có
   thể escalate lên user qua vai trò tổng hợp. Quyết định dừng/tiếp tục luôn thuộc về
   user, không phải agent giám sát.
8. Tổng hợp/báo cáo — gộp kết quả 5 phase + phản biện thành 1 báo cáo cuối cho user,
   nêu rõ mọi trường hợp giám sát đã escalate và user cần quyết định gì.

Mỗi phase executor phải hỏi user trước khi commit/push (không tự ý — theo KHÔNG ĐƯỢC
LÀM). Vai trò giám sát KHÔNG có quyền tự sửa code của phase executor — chỉ phản biện,
báo cáo lên vai trò tổng hợp.
