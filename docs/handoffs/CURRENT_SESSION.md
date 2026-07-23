# Current Session Handoff

## Deliverable hiện tại

Đóng 4 gap đã kiểm chứng giữa vision Autonomous SRE (Omni=não, Remote Agent=chân/tay/mắt) và
code thật, qua blueprint plan 5 phase (thứ tự chốt: 3→4→2→1→5), thực thi bằng Workflow 8 vai
trò (5 phase executor + blueprint + giám sát/phản biện + tổng hợp).

## Definition of Done

- Plan `plans/omni-close-autonomous-sre-gaps-2026-07-23.md` hoàn chỉnh, đã qua adversarial
  review (Opus) và sửa hết CRITICAL/HIGH findings.
- Cả 5 phase (3: confidence boundary, 4: action library, 2: RAG grounding, 1: question
  boundary, 5: E2E nghiệm thu) chạy xong, mỗi phase có giám sát/phản biện xác nhận.
- Báo cáo tổng hợp cuối liệt kê đầy đủ file thay đổi + escalation cần user quyết định.

**Trạng thái: ĐANG CHẠY** (background Workflow) — chưa DONE.

## Trạng thái hiện tại

- Đã hỏi user 6 câu hỏi quyết định (thứ tự phase, commit trước, phạm vi action library,
  số vai trò orchestration) — tất cả đã chốt, xem "Quyết định đã chốt" bên dưới.
- Đã phát hiện VÀ SỬA 1 lệch tiền đề tại chỗ trước khi launch: audit cũ nói action library
  "chỉ có 1 capability" — thực tế đã có 3 (`systemd_restart`, `systemd_reset_failed`,
  `systemd_journal_vacuum`, 2 cái sau mới thêm 2026-07-21). User xác nhận giữ nguyên kế
  hoạch (3-5 capability mới, domain remote-host/VM) coi 3 cái hiện có là nền.
- Plan đã qua 1 vòng adversarial review (agent `architect`, model Opus) — tìm thấy 1
  CRITICAL (C1: Phase 3↔Phase 4 có dependency CONTRACT thật qua `VerificationResult`,
  không độc lập như bản nháp đầu claim) + 3 HIGH (H1: Phase 1 premise sai — code đã tự
  ghi rõ ranh giới, không phải trùng lặp cần xoá; H2: Phase 1 import từ file Phase 3 sở
  hữu; H3: nhánh "hợp nhất" ở Phase 3 có rủi ro vi phạm `INV_DERIVED_NEVER_PERSIST`; H4:
  ví dụ capability "network interface reset" ở Phase 4 không thể rollback) + vài
  MEDIUM/LOW. Đã sửa hết trong file plan trước khi launch Workflow.
- **Workflow đang chạy nền**: task ID `wj82bbipp`, run ID `wf_1e153f17-f7b`. Theo dõi bằng
  `/workflows`. Script lưu tại
  `.claude/projects/-Users-hiendang-project/182f63ce-8b7f-4c5b-adbd-075053c8358b/workflows/scripts/omni-close-autonomous-sre-gaps-wf_1e153f17-f7b.js`.
  Thứ tự thực thi: Phase 3 (Opus) → Critic → Phase 4 → Critic → Phase 2 (Opus) → Critic →
  Phase 1 → Critic → Phase 5 (E2E) → Critic → Synthesis (báo cáo cuối).

## Đã hoàn thành (trước khi launch Workflow)

- Commit 2 phần working tree kế thừa từ phiên trước (đã hỏi user, được duyệt "Có — commit
  ngay"): `c7a1ed1` (SIEM/security audit follow-up, 8 file `src/`/`tests/`, 188 test pass)
  và `bae1781` (docs consolidation 248→73 file, 191 file thay đổi).
- Viết plan 5 phase đầy đủ (context brief, exit criteria đo được, rollback, dependency
  graph) tại `plans/omni-close-autonomous-sre-gaps-2026-07-23.md`.
- Chạy adversarial review, sửa toàn bộ CRITICAL/HIGH/MEDIUM finding vào file plan.
- Launch Workflow 8 vai trò thực thi plan.

## Branch và commit

`main`. HEAD `bae1781`. Working tree hiện tại: chỉ còn `plans/` (untracked, file plan mới) —
sạch, không có gì dở dang từ trước. Workflow đang chạy CÓ THỂ tạo thêm thay đổi trong
`src/aoip/`, `src/services/`, `tests/`, `docs/architecture/` khi hoàn tất — session sau
PHẢI chạy `git status` để xem thực tế, KHÔNG giả định dựa vào handoff này (Workflow chạy
nền, có thể xong sau khi handoff này được ghi).

## Files chính liên quan

- `plans/omni-close-autonomous-sre-gaps-2026-07-23.md` — plan đầy đủ, nguồn sự thật cho
  scope/exit-criteria/rollback từng phase.
- File dự kiến bị Workflow đụng (theo plan, xác nhận thật bằng `git status` khi Workflow
  xong): `src/aoip/verification.py`, `src/aoip/competency_matrix.py` (Phase 3);
  `src/aoip/capabilities/*.py` mới (Phase 4); `src/services/knowledge/document_store.py`,
  `src/services/analyst/`, `src/rag/` (Phase 2); `src/aoip/question_lifecycle.py`,
  `src/workers/onboarding_pipeline.py` (Phase 1); `tests/e2e_*` mới (Phase 5).

## Quyết định đã chốt (KHÔNG thiết kế lại)

- Thứ tự phase: 3→4→2→1→5 (rủi ro thấp/kiến trúc trước, năng lực sau, E2E cuối).
- Commit working tree trước khi bắt đầu — đã làm (`c7a1ed1`, `bae1781`).
- Action library: 3-5 capability mới, domain remote-host/VM trước (không phải K8s).
- Orchestration: 8 vai trò đầy đủ qua Workflow (không rút gọn xuống 6).
- Phase 1 mặc định: xác nhận + chính thức hoá ranh giới (KHÔNG migrate-and-delete trừ khi
  điều tra thật lộ bằng chứng ngược lại).
- Phase 3 mặc định: giữ ranh giới rõ giữa `VerificationResult`/`FacetState` (KHÔNG hợp
  nhất trừ khi bảng field-by-field chứng minh trùng lặp thật; nếu hợp nhất, cấm persist
  `FacetState`).
- Phase 4: loại bỏ "network interface reset" khỏi danh sách candidate (không rollback được).
- Vai trò giám sát/phản biện: chỉ có quyền escalate lên user qua vai trò tổng hợp, KHÔNG
  tự chặn/sửa code executor.

## Verification đã chạy

```
.venv/bin/python -m pytest tests/test_chain_consumer.py tests/test_remote_agent_command_executor.py \
  tests/test_siem_unified_pipeline.py -q → 188 passed (trước commit c7a1ed1)
```

Verification cho 5 phase trong Workflow: xem báo cáo tổng hợp cuối (Synthesis agent) khi
Workflow hoàn tất — mỗi phase executor được yêu cầu tự chạy pytest liên quan và báo cáo
pass/fail count, critic được yêu cầu tự xác nhận lại (không tin lời khai).

## Deployment hiện tại

N/A — chưa có thay đổi runtime/deploy trong phiên này.

## Blockers

Không có blocker tại thời điểm ghi handoff này. Workflow đang chạy nền — CÓ THỂ phát sinh
escalation (xem báo cáo Synthesis) cần user quyết định khi hoàn tất, ví dụ nếu 1 phase có
critic verdict = BLOCK hoặc CONCERN.

## Next step chính xác

1. Kiểm tra trạng thái Workflow: `/workflows` hoặc chờ thông báo hoàn tất.
2. Đọc báo cáo tổng hợp cuối (Synthesis agent output) — chứa bảng tóm tắt 5 phase, danh
   sách file thay đổi, danh sách escalation cần quyết định.
3. Nếu có escalation/BLOCK: đọc kỹ, quyết định cùng user trước khi cho phép tiếp tục hoặc
   sửa lại.
4. Chạy `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` để xác nhận toàn
   bộ 5 phase không phá test suite hiện có.
5. Review `git status --short` + `git diff --stat` — quyết định commit (có thể tách theo
   phase hoặc gộp), KHÔNG tự ý commit/push mà không hỏi user trước (Workflow agent cũng
   được yêu cầu không tự commit).

## Lệnh cần chạy lại

```
git status --short                                                # xem thay đổi thật sau Workflow
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration    # xác nhận không regression
cat plans/omni-close-autonomous-sre-gaps-2026-07-23.md             # đọc lại plan gốc nếu cần đối chiếu
```

## Không được làm lại

- Đừng audit lại 18-domain hay `docs/` từ đầu (đã đóng ở phiên trước, xem
  `project_18domain_capability_audit_2026_07_22` và `project_docs_consolidation_2026_07_22`
  trong memory).
- Đừng mở lại design đã freeze (`docs/architecture/FRAMEWORK_LAWS.md` và Constitution liên
  quan) trừ khi 1 trong 5 phase chứng minh bắt buộc — nếu Workflow báo cáo escalation loại
  này, đọc kỹ lý do trước khi quyết định, không tự ý mở lại.
- Đừng đổi lại thứ tự phase 3→4→2→1→5 — đã chốt có lý do (dependency contract thật giữa
  Phase 3 và Phase 1/4, xem plan phần "Dependency graph").
- Đừng tự ý commit/push kết quả Workflow mà không cho user xem báo cáo tổng hợp trước.

## Tài liệu liên quan

- `plans/omni-close-autonomous-sre-gaps-2026-07-23.md` — plan đầy đủ, đã qua adversarial
  review, nguồn sự thật cho phiên này.
- `docs/architecture/FRAMEWORK_LAWS.md` — Constitution, không đổi trong phiên này.
- `CLAUDE.md` — nguồn sự thật kiến trúc, không đổi trong phiên này.

## Phase 4 DONE (2026-07-23) — Action library remote-host/VM (+3 capability)

Re-read `verification.py` sau Phase 3 (shape KHÔNG đổi) rồi thêm 3 capability mới trong
`src/aoip/capabilities/`: `systemd.kill_unit` (SIGTERM qua `systemctl kill`, hồi phục dựa
Restart= của unit), `systemd.disk_cleanup` (target cố định
`systemd-tmpfiles-clean.service`, ngưỡng %-usage env-configurable), `systemd.config_rollback`
(cp từ `<path>.aoip-backup`, path resolve server-side qua `AOIP_CONFIG_ROLLBACK_PATHS`,
snapshot `<path>.pre_rollback_snapshot` TRƯỚC khi ghi đè — reversibility proof). Loại bỏ
"package pin/rollback qua command_executor.py" khỏi danh sách vì module đó là
METADATA-ONLY/`INV_NO_WRITE` (đọc source xác nhận) — không dùng được cho mutation; thay
bằng recovery.py operator pattern giống 3 capability cũ. 3 operator mới đăng ký ở
`aoip.recovery.OPERATORS`; risk class LOW cho cả 3 trong `pkg/risk_taxonomy.py`. Verify
non-mutating trên VM lab thật (`orb -m cust-app`): `df --output=pcent /`, `systemctl show -p
MemoryCurrent --value`, `systemctl show -p LoadState --value systemd-tmpfiles-clean.service`,
`sha256sum` — tất cả khớp parser. Test: 84 test mới (3 file), full suite
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` → 6634 passed (từ 6550,
+84), 0 fail. CHƯA commit — chờ user duyệt. File thay đổi: `src/aoip/recovery.py`,
`src/aoip/capabilities/systemd_{kill_unit,disk_cleanup,config_rollback}.py`,
`src/pkg/risk_taxonomy.py`, `tests/test_capability_systemd_{kill_unit,disk_cleanup,
config_rollback}.py`.

## Phase 2 DONE (2026-07-23) — Nối tài liệu nghiệp vụ khách hàng vào advisory reasoning

`ingest_customer_knowledge()` (`document_store.py`) lưu metadata+summary (≤2000 chars)
nhưng chưa ai đọc lại. Thêm `workers/customer_knowledge_context.py::
build_customer_knowledge_block()` (pattern giống `system_twin_context.py`: fail-open,
capped max_chars, đọc `list_docs()` — metadata/summary ONLY, KHÔNG bao giờ full content,
giữ `INV_DATA_RESIDENCY`). Header block tự mang disclaimer "CHƯA qua verify — coi là gợi ý
tham khảo (customer-provided, chưa verify)" ngay trong evidence text (không đụng system
prompt — budget đã sát trần, kitchen-sink test chỉ còn 7 chars dư, thêm section mới vào
system prompt làm vỡ `test_advisory_prompt_budget.py`; đã revert phần đó, giữ disclaimer ở
tầng evidence là đủ cho verify-before-believe/`INV_LLM_NOT_FIRST`). Wired vào
`evidence_consumer.py` ngay trước RAG second-brain + LLM advisory call (grep xác nhận thứ
tự: `build_customer_knowledge_block` dòng ~2792 < `run_advisory_analyst` dòng ~2848). Test
mới: `tests/test_customer_knowledge_context.py` (6 test, TDD RED→GREEN). `make
benchmark-advisory` xanh (schema gate 138 passed, live-benchmark 1 passed). Full suite
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` → 6640 passed (từ 6634,
+6), 0 fail. CHƯA commit — chờ user duyệt. File thay đổi: `src/workers/
customer_knowledge_context.py` (mới), `src/workers/evidence_consumer.py` (wire-in),
`tests/test_customer_knowledge_context.py` (mới).

## Phase 5 DONE (2026-07-23) — E2E tiêu chí nghiệm thu "Senior SRE nhận bàn giao"

Precondition: Phase 3/4/2/1 đều `exit_criteria_met=true`, critic verdict PASS — không có
blocker nào phải escalate trước khi chạy E2E.

`tests/test_phase5_e2e_senior_sre_handoff.py` (6 test, marker `integration` — không chạy
trong suite mặc định) chạy 6 bước THẬT trên VM lab `cust-app` (qua `orb -m`, transport SSH
thật `RealSSHTransport`, KHÔNG FakeSystemd): (1) Discover — `systemctl list-units` thật
thấy unit throwaway `e2e-phase5-demo.service` tự tạo/tự xoá trong module fixture; (2) Hỏi
người — `sync_unknowns_from_competency` + `ensure_question_for_unknown` (facet `runbook`
UNKNOWN thật); (3) Nhận trả lời — `submit_answer` với script giả lập (cho phép rõ theo
plan), chiếu đúng predicate `has_runbook` → Claim qua `claims_store`; (4) Verify — probe
THẬT `systemctl show -p Restart --value` trên VM, so khớp claim (không tin lời khai); (5)
Thực thi — `systemd_kill_unit` executor thật, preflight thật (unit_exists = real SSH
round-trip), CHỈ chạy tới `MODE_SHADOW`/observe_only theo đúng giới hạn an toàn của
orchestrator ("chỉ observe_only/shadow, KHÔNG bypass gate để test nhanh") — KHÔNG tự
approve/chạy `MODE_HUMAN_APPROVED` thật (SIGTERM thật) lên VM; cũng verify riêng gate
fail-closed khi `approved=False` (từ chối trước cả bước shadow); (6) Báo cáo — JSON tổng
hợp in ra log test, không giấu gap.

**Kết quả**: 6/6 bước chạy thật PASS, trừ 1 giới hạn CÓ CHỦ Ý (không phải gap Phase 1-4):
bước thực thi dừng ở shadow/observe_only, không tiến tới mutate thật (SIGTERM) trên VM lab
— cần user xác nhận nếu muốn chạy tiếp bước đó (target là unit throwaway, phục hồi được
qua `Restart=always`, rủi ro thấp nhưng vẫn là mutate thật nên không tự ý). VM lab đã dọn
sạch sau test (unit + file service đã xoá, xác minh qua `systemctl status` → not found).
Full suite `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` → 6640 passed,
11 deselected (integration, gồm 6 test mới), 0 fail — không giảm so với trước phase. CHƯA
commit — chờ user duyệt. File thay đổi: `tests/test_phase5_e2e_senior_sre_handoff.py` (mới).

## TỔNG KẾT 5 PHASE — WORKFLOW DONE, ĐÃ TỰ VERIFY ĐỘC LẬP + COMMIT

Cả 5 phase (3→4→2→1→5) DONE, exit criteria đạt, 5/5 critic PASS (0 CONCERN/BLOCK). Sau khi
Workflow hoàn tất, tôi (không phải executor con) tự chạy lại độc lập: `git log` xác nhận
không có commit lén; full suite `pytest tests/ -q --ignore=tests/integration` → **6640
passed, 11 deselected, 0 fail** (khớp chính xác báo cáo của mọi phase/critic).

**Bước mutate thật MODE_HUMAN_APPROVED (do user xác nhận chạy tiếp)**: chạy script one-off
(`/private/tmp/.../scratchpad/run_phase5_mode_human_approved.py`, không phải file trong repo)
lên unit throwaway `e2e-phase5-demo-approved.service` trên VM lab `cust-app` qua
`systemd_kill_unit` executor thật, `mode=MODE_HUMAN_APPROVED`, approval thật. Kết quả: SIGTERM
thật được gửi — `MainPID` đổi `21386`→`21412`, xác nhận unit đã bị kill và tự respawn qua
`Restart=always` (self-healing hoạt động đúng). Verification báo `ESCALATED` vì tôi cố tình
đặt `AOIP_KILL_UNIT_MEMORY_THRESHOLD_BYTES=1000` (demo, ép `is_broken=True` ngay) — process
sau respawn vẫn dùng >1000 bytes nên health-check fail đúng thiết kế (fail-closed, không phải
bug). VM đã dọn sạch, xác nhận `LoadState` không còn `loaded` sau cleanup.

**Đã commit** theo lựa chọn "gộp 1 commit" của user — xem `git log -1` để lấy hash thật.
Working tree sau commit: sạch (trừ file không thuộc repo trong `/private/tmp/.../scratchpad/`).
