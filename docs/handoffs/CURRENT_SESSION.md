# Current Session Handoff

## Deliverable
**Vertical slice: nối AOIP durable command → `run_guarded_recovery` → outcome → durable report.**
Thay `_noop_executor` mặc định bằng adapter production-safe (`operations.build_recovery_executor`).
Không mở capability mới, không sửa deployment/atomic-claim/payload-hash/gateway.

## Pre-change contract map
- Daemon executor (`delivery_loop.Executor`): `async (payload: dict) -> (terminal_state, outcome_dict)`.
  `payload` = free-form dict từ durable command's `payload` field (Gateway không biết domain aoip).
- `run_guarded_recovery(ctx, req, transport, audit_log, gate, approval, env_auto_execute, now, redis,
  holder, probe_dependent=None, lease_ttl_s=120) -> RecoveryOutcome`. Cần `ctx.findings` +
  `ctx.diagnosis_confidence` + `ctx.log()` (duck-typed, KHÔNG cần `UnderstandingContext` đầy đủ).
- Ledger states: `CLAIMED` → `STATUS_TERMINAL` (`recovered|escalated|aborted|completed`).
  Claim xảy ra TRƯỚC `execute_recovery`; record terminal chỉ khi `recovered`/`escalated`; `aborted`
  → `release_claim` (cho retry hợp lệ sau). Lease KHÔNG có auto-renewal (chỉ TTL 120s cố định).
- Ambiguous outcome KHÔNG lộ ra ngoài `run_guarded_recovery`: nó tự resolve nội bộ qua current-state
  revalidation trong `execute_recovery` trước khi return — mỗi lần gọi luôn trả outcome xác định.

## Implementation
- **`src/aoip/agent/operations.py`** (mới, cuối file):
  - `decode_recovery_command(payload)` — parse `payload["recovery"|"approval"|"evidence"]` →
    `(RecoveryRequest, Approval, _EvidenceCtx)`. Raise `UnsupportedRecoveryPayload` fail-closed nếu
    thiếu field hoặc không có operator cho `(failure_mode, substrate)`.
  - `_EvidenceCtx` — carrier tối thiểu (findings/diagnosis_confidence/log), KHÔNG noun mới.
  - `_map_recovery_outcome(outcome)` — `recovered`→COMPLETED; `aborted`+"HEALTHY" trong reason→
    COMPLETED+`NO_ACTION_NEEDED`; `escalated`→ESCALATED; `aborted` khác (gate/lease/approval chặn)→FAILED.
  - `build_recovery_executor(redis, holder, transport, audit_log, gate, env_auto_execute=False,
    lease_ttl_s=120, probe_dependent=None, now=None)` — adapter hẹp, KHÔNG bypass lease/ledger.
    Exception bất kỳ (transport/redis) → FAILED rõ ràng, không bao giờ COMPLETED ngầm.
- **`src/aoip/agent/daemon.py`**: `run_daemon`/`main` nhận thêm `redis, transport, audit_log, gate,
  env_auto_execute, now`. Nếu đủ 4 dep đầu → default executor = `build_recovery_executor`; nếu KHÔNG
  → fallback `_noop_executor` (docstring đổi: DEV/TEST-ONLY, không phải production default khi đã đủ dep).
- **`tests/test_aoip_operations.py`**: +13 test (decode contract, happy path, duplicate delivery,
  already-healthy no-op, verification-failure escalate, expired approval, unsupported payload,
  transport exception, daemon wiring end-to-end).

## Safety semantics
- Mutation chỉ thực hiện khi: payload decode được + qua toàn bộ gate (`_gate_checks` trong
  `recovery.py`) + current-state xác nhận còn hỏng.
- `COMPLETED` hợp lệ khi: (1) mutation + verify service/dependents pass, hoặc (2) current-state gate
  thấy đã khỏe trước mutation → `NO_ACTION_NEEDED` + evidence, KHÔNG mutation.
- `FAILED`: payload không decode được / gate chặn (approval sai-hạn-tenant-scope, risk, capability) /
  exception trong `run_guarded_recovery`. KHÔNG COMPLETED trong mọi nhánh này.
- `ESCALATED`: verify sau mutation fail (đã thử 1 lần, KHÔNG retry vô hạn — domain semantics có sẵn
  trong `execute_recovery`, giữ nguyên, không đổi).
- Duplicate delivery / crash-after-claim: `run_guarded_recovery` tự dedup qua `IdempotencyLedger` +
  `ExecutionLease`; executor gọi lại bao nhiêu lần cũng an toàn (effectively-once, KHÔNG tuyên bố
  exactly-once tuyệt đối — lease/ledger/mutation không cùng transaction).

## Test results
| Command | Result |
| --- | --- |
| `pytest tests/test_aoip_operations.py tests/test_aoip_agent_daemon.py tests/test_aoip_delivery.py tests/test_aoip_intake.py -q` | 51 passed |
| `pytest tests/ -q -k aoip` | 187 passed |
| Full suite / lint | Not run (out of time budget phiên này) |

## Remaining gaps (không đổi so với trước, xác nhận lại)
- **Atomic claim**: `poll_commands` vẫn 4 op rời (chưa Lua/WATCH) — double-DELIVERED risk còn, đã có
  lease+ledger backstop mutation-safety nên KHÔNG blocker.
- **Gateway transition enforcement bất đối xứng**: `_advance` chưa validate expected-current-state.
- **`payload_hash` chỉ metadata**: chưa canonical-hash + verify hai chiều.
- **CLI chưa wiring production**: `daemon.main()` KHÔNG có flag `--redis-url`/`--audit-path`/gate
  config → khi chạy thật qua CLI vẫn rơi về `_noop_executor` (không đủ 4 dep). `run_daemon()` đã sẵn
  sàng nhận dep qua code; nối CLI = việc kế tiếp, không thuộc scope "connect executor" phiên này.
- **Lease KHÔNG renewal**: TTL cố định 120s, mutation dài hơn TTL có thể mất lease giữa chừng — chưa
  test riêng case này (đã note trong spec Bước 6.E, chưa viết test).
- **`command_identity` (immutable IDs) không được dùng bởi `run_guarded_recovery`**: nó vẫn dùng
  `idempotency_key` (legacy, theo tenant+scope+decision+failure_mode+unit) — gap tồn tại từ trước,
  KHÔNG do slice này tạo ra, KHÔNG sửa trong scope này (đổi sẽ động ledger key production).

## Next hardening slice
Atomic delivery claim (Lua script cho `poll_commands`) hoặc Gateway transition enforcement — chọn
theo dependency thực tế lúc bắt đầu, không quyết định trước.

## Git status
Chưa commit. Files changed: `src/aoip/agent/operations.py`, `src/aoip/agent/daemon.py`,
`tests/test_aoip_operations.py`, `docs/handoffs/CURRENT_SESSION.md`.
