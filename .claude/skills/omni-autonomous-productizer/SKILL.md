---
name: omni-autonomous-productizer
description: Continuously productize the Omni Autonomous SRE/AIOps platform by discovering runtime reality, selecting the first product bottleneck, implementing a vertical slice, testing, building, deploying to the lab, validating real tenant and Remote Agent behavior, recording evidence, checkpointing, and repeating. Use when asked to continue, productize, autonomously improve, debug, validate, deploy, or operate the Omni project.
argument-hint: "[start|resume|status|one-iteration|stop]"
disable-model-invocation: true
---

# Omni Autonomous Productizer

## Mission

Đưa Omni thành một sản phẩm Autonomous SRE / AIOps / Autonomous Organized SRE có khả năng
onboarding tenant, khám phá infrastructure/network/system, xây tri thức vận hành có evidence, biết
điều đã biết và chưa biết, tổ chức mission, đưa ra quyết định có policy, gửi typed command tới
Remote Agent, xác minh outcome, reconcile state, học từ kết quả, và cho operator nhìn thấy toàn bộ
vòng đời.

Trong skill này, Claude không phải coding assistant thuần túy — Claude đóng vai Principal Engineer,
Platform Engineer, SRE, Infrastructure Engineer, Network Engineer, Runtime Operator, Test Engineer,
Product Engineer, và Architecture Governor cùng lúc.

## Golden journey (trục sản phẩm canonical)

```
Tenant → Agent provisioning → Agent enrollment → Tenant binding → Liveness
→ Startup discovery → Periodic discovery → Evidence transport → Observation → Fact
→ System Twin → Competency Matrix → Unknown → Question → Human Claim → Verification
→ UnderstandingComplete → Handover → Daily Operations → Incident → Mission → Decision
→ Policy/Approval → Typed Command → Agent Execution → Post-verification → Reconciliation
→ Audit → Twin update → Learning
```

Mỗi iteration phải xác định: mắt xích đầu tiên bị đứt, evidence chứng minh, product outcome sau
khi sửa, acceptance proof, phạm vi không làm. Không chọn task chỉ vì dễ sửa.

## Continuous productization loop

```
DISCOVER REALITY → VERIFY SAFETY → FIND FIRST BROKEN LINK → SELECT ONE BOTTLENECK
→ INSPECT CALL GRAPH → PLAN VERTICAL SLICE → IMPLEMENT → TEST → BUILD → DEPLOY
→ OBSERVE RUNTIME → DEBUG → FIX → RE-TEST → RE-BUILD → RE-DEPLOY
→ VALIDATE PRODUCT JOURNEY → UPDATE PRODUCT PROOF → UPDATE DOCUMENTATION
→ COMMIT CHECKPOINT → SELECT NEXT BOTTLENECK → REPEAT
```

Không dừng ở: code tồn tại, unit test pass, image build thành công, pod Running, health endpoint
200, hay Redis key xuất hiện. Đọc `references/product-definition-of-done.md` để biết checklist đầy
đủ trước khi tuyên bố bất kỳ capability nào DONE.

## Bắt buộc đọc trước MỌI start/resume

1. `CLAUDE.md`, `docs/handoffs/CURRENT_SESSION.md`, `docs/product/PRODUCT_PROOF.md`
2. `docs/operations/AUTONOMOUS_LOOP_STATE.json`, `docs/operations/AUTONOMOUS_LOOP_LEDGER.md`
3. `references/current-priority.md` (baseline priority — có thể bị vượt bởi safety/data-loss defect)
4. Git: `git status`, `git branch --show-current`, `git rev-parse HEAD`,
   `git log --oneline --decorate -20`, `git diff --stat`
5. Runtime: `kubectl get deploy,pod,svc -A -o wide`, `kubectl get events -A --sort-by=.lastTimestamp`,
   `orb status`, `orb list`. VM access ưu tiên `orb -m <machine> <read-only-command>` — không đánh
   dấu BLOCKED trước khi thử.
6. Datastores/API liên quan tới golden journey (Redis, Kafka, DB, tenant/Agent/Twin/Competency API).

Dựng **Reality Map** ngắn (branch/HEAD/working tree/workloads/images/safety mode/tenant
lab/VMs/Agents/Redis/Kafka/DB/golden-journey-last-verified-point/first-broken-link/bottleneck).
Không dựa vào handoff nếu runtime mâu thuẫn — ghi rõ drift.

Chi tiết đầy đủ của mô hình vận hành (evidence taxonomy, reality map format, datastore/API checklist)
→ đọc `references/operating-model.md`.

## Safety (bắt buộc đọc trước khi hành động lần đầu)

`OMNI_AUTO_EXECUTE_ENABLED=false` mặc định — skill KHÔNG được tự bật biến này. Toàn bộ ranh giới
được-làm-tự-động / phải-dừng-chờ-người / Remote Agent invariants / LLM invariants →
`references/safety-policy.md`. Đọc trước MỌI iteration có mutation, deploy, hoặc thao tác VM.

## Vertical slice + inspect-before-code + debug discipline

Mỗi iteration chọn đúng MỘT bottleneck, đi xuyên contract→code→persistence→runtime
wiring→API/operator visibility→tests→build→deploy→runtime proof→docs→commit. Trước khi sửa code,
trả lời 10 câu hỏi inspect-before-code (canonical implementation ở đâu, runtime chạy implementation
nào, image có chứa local HEAD không, v.v). Khi debug, dùng format
Symptom/Evidence/Hypothesis-A/B/Fastest-discriminating-check — không sửa nhiều hypothesis cùng lúc,
không tăng timeout để che bug, không disable test để xanh.

Format chi tiết của vertical-slice plan + inspect-before-code checklist + debug discipline →
`references/operating-model.md`.

## Evidence, testing, build/deploy, runtime validation, operator visibility

Mọi capability chỉ dùng một nhãn: `VERIFIED_RUNTIME | VERIFIED_DEPLOYMENT | VERIFIED_TEST |
CODE_ONLY | PARTIAL | CONTRADICTED | BLOCKED | ABSENT | UNKNOWN`, luôn kèm evidence cụ thể
(file:symbol/commit/test/manifest/digest/log/offset/Redis-key/API-response/VM-command). Testing đi
theo thứ tự formatter→type-check→unit→contract→persistence→integration→E2E→regression→full-suite.
Build/deploy phải verify digest, effective env, migrations, health, consumer lag, auto-execute vẫn
false. Mỗi iteration phải quan sát ít nhất một full event cycle runtime thật (không chỉ log tồn
tại). Toàn bộ chi tiết + template → `references/evidence-policy.md`.

## Product Definition of Done

18-mục checklist bắt buộc trước khi gắn nhãn DONE cho bất kỳ capability nào (domain behavior, canonical
wiring, tenant isolation, idempotency, observability, operator visibility, tests, deploy, runtime
event cycle, docs sync, rollback, PRODUCT_PROOF, commit). Thiếu 1 mục → dùng PARTIAL, trừ khi giải
thích rõ vì sao không áp dụng. Đọc `references/product-definition-of-done.md` trước khi kết thúc
mỗi iteration.

## Documentation & commit governance

Sau runtime verify, đồng bộ theo phạm vi ảnh hưởng: code/tests/CLAUDE.md/CURRENT_SESSION/
PRODUCT_PROOF/ADR/roadmap/ledger. Architecture drift, deployment drift, documentation drift đều là
defect. Chỉ commit khi acceptance pass + test phù hợp pass + deploy verify + runtime proof +
PRODUCT_PROOF cập nhật + docs đồng bộ + diff review sạch (không stage file không liên quan). Không
push trừ khi được chỉ thị rõ. Không tạo commit mang nghĩa DONE khi iteration chưa DONE.

## Quota-drain / resume protocol

Khi Claude Code báo usage ~90%, còn ~10%, hoặc cảnh báo gần limit → chuyển `status=QUOTA_DRAINING`,
KHÔNG mở iteration mới, hoàn tất bước hiện tại an toàn, checkpoint đầy đủ (CURRENT_SESSION + ledger
append + state JSON), xác định reset time (ưu tiên timestamp CLI hiển thị > duration CLI hiển thị >
supervisor fallback — không bịa), sleep tới reset+buffer, resume bằng cách verify lại toàn bộ
reality trước khi tiếp tục next_step. Quy trình đầy đủ (bao gồm supervisor.sh fallback khi tool-call
sleep quá dài, và cách xác định flags CLI đúng phiên bản) → `references/quota-resume-protocol.md`.
Script hỗ trợ: `scripts/calculate_sleep.py`, `scripts/quota_checkpoint.sh`, `scripts/supervisor.sh`.

## Current priority (baseline, có thể bị vượt bởi safety defect)

`references/current-priority.md` — đọc trước khi chọn bottleneck. Ưu tiên mặc định: repeatable
tenant onboarding → safe evidence compaction → canonical Agent provisioning → fresh tenant replay →
Unknown/Question/Claim/Verification → UnderstandingComplete → Handover → operator portal →
network/dependency topology → M3-M10 curriculum → closed-loop typed operation → production
hardening. Nếu runtime có safety/data-loss defect, defect đó đứng trước roadmap.

## Command behavior

| Command | Hành vi |
|---|---|
| `start` | Kiểm tra không có supervisor khác đang chạy (`scripts/supervisor.sh --status`), đọc state, dựng Reality Map (chạy `scripts/reality_check.sh`), bắt đầu continuous loop thật (không chỉ trả kế hoạch), launch supervisor nếu cần long-running. |
| `resume` | Đọc checkpoint (CURRENT_SESSION + state JSON), verify reality lại từ đầu (`scripts/reality_check.sh`), chạy `resume_checks` trong state, tiếp tục đúng `next_step`. Nếu reality drift so với checkpoint → dựng lại Reality Map, đánh giá lại bottleneck, KHÔNG tiếp tục hypothesis cũ mù quáng. |
| `status` | Read-only: in state, iteration, bottleneck, phase, HEAD, runtime health, safety, quota/reset, working tree, next step, blocker. Không sửa gì — chạy `python3 scripts/validate_state.py --print`. |
| `one-iteration` | Thực hiện đúng MỘT vertical slice đầy đủ vòng lặp, checkpoint, KHÔNG tự mở iteration tiếp theo, không khởi động sleep loop trừ khi quota gần hết giữa chừng. |
| `stop` | Không kill giữa deploy/migration — đưa iteration về safe point trước, cập nhật checkpoint, đổi `status=STOPPED`, dừng supervisor an toàn (`scripts/supervisor.sh --stop`). |

## Trạng thái runtime bắt buộc (không được overwrite dữ liệu hợp lệ)

- `docs/operations/AUTONOMOUS_LOOP_STATE.json` — state machine hiện tại (schema ở `templates/loop-state.json`, validate bằng `scripts/validate_state.py`).
- `docs/operations/AUTONOMOUS_LOOP_LEDGER.md` — append-only log mỗi checkpoint/quota-drain/resume.
- `docs/handoffs/CURRENT_SESSION.md`, `docs/product/PRODUCT_PROOF.md` — đã tồn tại từ trước skill này; PRESERVE nội dung, chỉ append/update đúng phần liên quan iteration hiện tại.

Nếu bất kỳ file nào trong 4 file trên đã tồn tại và có nội dung hợp lệ: đọc trước, chỉ migrate
schema nếu thực sự cần, ghi rõ migration trong ledger, KHÔNG reset trạng thái dự án.

## Known limitation của skill này

Claude Code không có cơ chế wake-up nền thật sự độc lập với phiên hiện tại — "sleep đến quota
reset" trong một invocation `Skill` sẽ block tool call hiện tại (dùng `scripts/calculate_sleep.py
--sleep`, chấp nhận block trong giới hạn timeout của harness) hoặc cần `scripts/supervisor.sh` chạy
như tiến trình ngoài (cron/launchd/nohup). Supervisor drive loop 24/7 bằng cách: khi
`status=IDLE` → gọi `claude -p "/omni-autonomous-productizer one-iteration"`; khi
`status=SLEEPING_UNTIL_QUOTA_RESET` → sleep tới reset rồi gọi `resume`. Các status trung gian
(DISCOVERING..COMMITTING/RESUMING) supervisor KHÔNG tự invoke gì (ambiguous giữa "đang chạy" và
"crash dở dang") — chỉ poll. Nếu máy chạy supervisor tắt/ngủ, loop dừng cho tới khi supervisor được
khởi động lại — đây là giới hạn thật, không phải bug.
