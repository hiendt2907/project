# Living Operations Runtime — Hardening Plan (pre-Gateway)

> Trạng thái: **PLAN — chưa code**. Chốt trước khi wiring Gateway/systemd.
> Nguồn: review P0/P1 của Slice 1 safety substrate. Không thêm kiến trúc mới —
> chỉ siết chặt nền idempotency/lease/approval/reconcile/audit đã có.

## Nguyên tắc xuyên suốt
- KHÔNG "exactly once". Chỉ **effectively-once qua idempotency + reconciliation**.
  Redis và mutation trên host KHÔNG chung transaction — luôn có cửa sổ crash.
- Fail-closed tuyệt đối trên production mutation path. Legacy/relaxed chỉ tồn tại
  trong test tường minh.
- KHÔNG noun ontology mới. Correlation IDs / execution-phase / fencing token là
  Derived runtime values (sổ vận hành), không persist như entity tri thức.

---

## SEQUENCING (revised) — hai track KHÔNG tách rời
Track A (Runtime Safety) và Track B (Operator Visibility) đi cùng nhau. Mọi runtime
state / safety gate / mutation phase / reconcile result / verify outcome ở Track A
PHẢI quan sát được ngay ở Track B. DoD mỗi commit backend: (1) cải thiện safety/capability
gì? (2) operator thấy & trace ở đâu? Không thấy được ngoài source/Redis/log/test = CHƯA xong.

- **Step 1** — P0 #1–7 + **Trace Spine** (emit event tương quan cho mọi transition; read-model mỏng).
- **Step 2** — Minimal Operations Trace Console (real data, không fixture): tenant/agent/mission/
  approval list + 1 timeline E2E + raw evidence + idempotency phase + lease holder/TTL/fencing + reconcile state + outcome.
- **Step 3** — Gateway/systemd vertical: operator xem live register→heartbeat→delivery→validate→approve→lease→mutate→verify→report→reconcile-after-restart.
- **Step 4** — Full Console: Tenant Overview / System Map / Mission Timeline / Incident Workspace / Human Inbox / Knowledge provenance.

### Trace Spine — event types (emit ở Step 1)
COMMAND_RECEIVED · IDEMPOTENCY_CLAIMED · LEASE_ACQUIRED · APPROVAL_VALIDATED · APPROVAL_REJECTED ·
MUTATION_STARTED · VERIFYING · RECONCILE_REQUIRED · RECONCILED · COMPLETED · ESCALATED · ABORTED.
Mỗi event có đủ: tenant_id, agent_id, mission_id, incident_id, decision_id, action_id, command_id,
canonical_scope, timestamp, state_before, state_after, reason, evidence_refs, correlation_id.

## BƯỚC 1 — P0 substrate hardening (#1–#7)

### #1 Bỏ mọi tuyên bố "exactly once"
- Files: `agent/operations.py`, `agent/idempotency.py`, `tests/test_aoip_operations.py`,
  `live_operations.py`.
- Đổi wording docstring/comment/print/test-name: "mutate đúng 1 lần" →
  "effectively-once (idempotency + reconciliation); Redis và host không chung transaction".
- Không đổi hành vi, chỉ ngữ nghĩa + đặt kỳ vọng đúng.

### #2 Execution-phase persistence (không nhả claim sau khi mutation có thể đã bắt đầu)
- `idempotency.py`: mở rộng trạng thái key:
  `CLAIMED → MUTATION_STARTED → VERIFYING → <terminal> | RECONCILE_REQUIRED`.
  Thêm hằng `STATUS_MUTATION_STARTED`, `STATUS_VERIFYING`, `STATUS_RECONCILE_REQUIRED`.
  Thêm `set_phase(key, *, phase, holder, meta)` (giữ holder + before-state ref).
- `operations.py`/`execute_recovery`: ghi `MUTATION_STARTED` **ngay trước** `op.apply()`,
  `VERIFYING` ngay sau apply, terminal sau verify. KHÔNG `release_claim` một khi phase ≥
  MUTATION_STARTED. Nếu gate chặn (còn ở CLAIMED, chưa apply) → được phép release.
- Restart/duplicate thấy phase ∈ {MUTATION_STARTED, VERIFYING} → **bắt buộc reconcile**
  (nhánh #7), KHÔNG blind re-dispatch.

### #3 Approval production strict fail-closed
- `recovery.Approval`: bỏ default rỗng khỏi **production path**. Thêm field bắt buộc:
  `tenant, canonical_scope, decision_goal, action_id, approver, issued_at, expires_at`.
- Thêm factory `Approval.issue(...)` validate đủ 7 ràng buộc, raise nếu thiếu.
- `_gate_checks`: bỏ escape-hatch `not approval.tenant or ...` và `not approval.decision_goal or ...`.
  Thiếu binding = **fail**, không phải "bỏ qua". Thêm check `approval_action_bound` (action_id),
  `approval_issued_before_now`.
- Legacy constructor (default rỗng, expiry ∞) chỉ giữ cho test cũ, đánh dấu `# legacy: tests only`.

### #4 Idempotency identity từ immutable delivery/runtime IDs
- `idempotency_key(...)`: đổi input thành
  `tenant_id + mission_id + incident_id + decision_id + action_id + command_id + payload_hash`.
  `payload_hash` = sha256 canonical payload (unit, verb, port, failure_mode, substrate).
- `RecoveryRequest`: thêm correlation IDs `mission_id, incident_id, decision_id, action_id,
  command_id` (Derived; mặc định sinh từ context khi chưa có Gateway).
- Kết quả: hai incident khác nhau, cùng failure_mode + cùng recovery plan → **khác key**
  (khác incident_id/command_id) → KHÔNG collide.

### #5 Canonical scope + tenant trong MỌI key
- Thêm `scope.py::canonical_scope(tenant, node)` → `"{tenant}:{normalized_node}"`.
- `lease_key` → `"lease:{tenant}:{canonical}"`; idempotency + audit đều nhúng tenant.
- Hệ quả: cùng target name ở 2 tenant → 2 lease/idempotency/audit key riêng biệt.

### #6 Lease renewal + ownership re-check + fencing epoch
- `run_guarded_recovery`: **ngay trước mỗi mutation step** gọi `lease.check_owner(target, token)`;
  fail → zero further mutation, audit `EV_RECOVERY_LEASE_DENIED`.
- Long op: `lease.refresh()` định kỳ (đã có) + ghi **fencing epoch/token** vào audit block
  của `EV_RECOVERY_EXECUTED` để phát hiện stale writer ở multi-step tương lai.
- `lease.py`: thêm `fencing_token(scope)` (monotonic epoch qua Redis INCR `fence:{key}`).

### #7 Action-specific reconciliation cho `process_down + systemd`
- `recovery.py`: `RecoveryOperator` thêm callable `reconcile(transport, unit, port, before)`.
- systemd reconcile phân biệt 5 kết quả, dựa `is-active` + `ActiveEnterTimestamp` so với
  before-state timestamp + port probe:
  - `already_succeeded`  → active + port open + ActiveEnterTimestamp > before → terminal recovered.
  - `still_activating`   → state `activating` → **OBSERVE (poll bounded), KHÔNG restart lại**.
  - `execution_failed`   → state `failed` → escalate.
  - `mutation_not_observed` → inactive + timestamp == before → an toàn re-apply (mutation chưa hiệu lực).
  - `outcome_unknown`    → probe lỗi → escalate, giữ bằng chứng, KHÔNG mutate.
- Reconcile được gọi khi restart gặp phase MUTATION_STARTED/VERIFYING (thay cho current-state gate ngầm).

### Tests bước 1 (bắt buộc, `tests/test_aoip_operations.py` + `test_aoip_recovery.py`)
1. crash **trước** mutation (phase CLAIMED) → re-run an toàn, mutate 1 lần.
2. crash **sau dispatch** (phase MUTATION_STARTED, service active) → reconcile `already_succeeded`, restarts==0.
3. crash **during activation** (state activating) → observe, KHÔNG restart lại.
4. timeout với remote outcome unknown → escalate/unknown, zero re-mutation.
5. hai incident khác nhau, plan giống hệt → 2 key khác nhau, cả hai chạy độc lập.
6. cùng target name ở 2 tenant → 2 lease/idempotency key, không chặn nhau.
7. lease hết hạn giữa lúc op đang chạy → ownership re-check fail → no further mutation.
8. approval hết hạn / bound thiếu (thiếu action_id/tenant/decision) → zero mutation.
9. redelivery **trước** và **sau** terminal report → trước: reconcile theo phase; sau: reconcile terminal.

---

## BƯỚC 2 — P1 audit + docs (#8–#9)

### #8 Audit host = edge buffer, forward về CRAT
- `audit.FileAuditLog`: docstring nêu rõ **edge buffer**, không phải source of truth.
- `trace_id` KHÔNG còn là `scope`. Mỗi block mang correlation IDs
  (`mission_id/incident_id/decision_id/action_id/command_id`).
- Thêm `audit_forward.py`: đẩy block host → CRAT source-of-truth hiện có (Gateway/Redis chain),
  correlate theo IDs. Idempotent forward (đã forward seq nào thì bỏ qua).

### #9 Roadmap/Ledger repo artifacts + dọn EXECUTION_MODEL
- Tạo `docs/ROADMAP.md` + `docs/LEDGER.md` (nguồn kỹ thuật trong repo).
- Chuyển nội dung ledger AOIP từ Claude memory → `docs/LEDGER.md`. Memory chỉ còn con trỏ.
- `docs/architecture/EXECUTION_MODEL.md`: gỡ header trạng thái mâu thuẫn (8-verb "done").

---

## BƯỚC 3 — Real Gateway / systemd vertical slice  ✅ IMPLEMENTED (2026-07-01)

> **Trạng thái:** durable delivery + ack + agent inbox + resume + systemd đã code + test
> (25 unit/integration, GET=peek qua ASGI thật). Proof trên K8s Gateway + 3 VM systemd:
> chạy `scripts/prove_durable_delivery.py` (5 case tự động + 3 case hạ tầng thủ công).
> Inspect kết luận: kênh cũ `/commands/{agent_id}` là **RPOP (pop-on-read)** cho command
> chẩn đoán READ-ONLY (fire-and-forget, không mutation → không P0). Command MUTATING trước
> đây KHÔNG có kênh durable nào → thêm surface mới `/webhook/agent/rt` (peek + ack lifecycle).
>
> - Gateway: `src/gateway/routes/agent_runtime.py` (self-contained, không import aoip).
> - Agent core: `src/aoip/agent/{delivery,inbox,delivery_loop,daemon}.py`.
> - systemd: `deploy/systemd/aoip-agent.service`.
> - Còn lại (follow-up): nối `executor` daemon vào recovery mutation thật (hiện no-op an toàn);
>   portal projection đọc `/webhook/agent/rt/commands/record` (nav stub sẵn).

- **Trước tiên** inspect delivery semantics `/commands/{agent_id}` hiện có.
  GIẢ ĐỊNH quan trọng: **GET ≠ ACK**. Command chỉ được ACK sau khi Gateway chấp nhận
  **durable terminal outcome**. Không ACK khi mới fetch.
- Wire `operations_loop` → `HTTPOmniClient` thật; mission state durable (sống qua Gateway/Redis restart).
- Agent chạy như **systemd service sống lâu**; resume/abandon mission khi agent restart.
- Demo 8 case E2E trên 3 VM thật qua TCP với Gateway đang chạy.

---

## Sau đó (milestone kế) — Observable AOIP Operations Console (Prompt 2)
Phụ thuộc correlation IDs + CRAT forward của Bước 1–2. 6 surface (Tenant Overview /
Agent Fleet / System Map / Mission Timeline / Incident Workspace / Human Inbox) chỉ đọc
Gateway/Redis/Mission/Graph/Registry/Evidence/CRAT hiện có; thêm read-model API mỏng;
approval từ console dùng bounded Approval THẬT; tenant isolation mọi query; mọi item có
correlation ID link về evidence gốc. **KHÔNG** làm trước khi Bước 1–3 xong.
