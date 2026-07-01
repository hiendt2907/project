# Current Session Handoff

## Deliverable hiện tại
Long-running execution safety: atomic execution-lease renewal + Gateway delivery
visibility heartbeat, wired qua một renewal coordinator dùng chung ở cả agent-execution
layer (`operations.run_guarded_recovery`) và agent-delivery layer (`DeliveryLoop`).

## Timing model (Bước 1)

| Timer | Owner | TTL hiện tại | Renewal | Hết hạn gây ra điều gì |
|---|---|---:|---|---|
| Delivery visibility | Gateway (`omni:cmd:ready` zset score + `record.visibility_deadline`) | 60s (`_VISIBILITY_S`) | MỚI: `POST /commands/heartbeat` | Redelivery — attempt/token mới được claim, dù agent cũ vẫn RUNNING |
| Execution lease | Agent (`lease:{scope}` Redis key, `aoip.agent.lease`) | 120s mặc định | MỚI atomic: `ExecutionLease.renew()` | Agent khác `acquire()` được → concurrent-mutation risk |
| Action timeout | Agent (`transport.run(..., timeout=)`) | 30s (restart op), 5s (probe) | N/A (hard subprocess timeout) | `ConnectionError`/timeout → `FAILED` |
| Verification timeout | Trong `execute_recovery` (probe `dev/tcp`) | 5s | N/A | probe fail → `escalated` |
| Agent heartbeat (control-plane) | Agent → Omni (`register`/`heartbeat()`) | presence-based, không TTL cứng | Periodic call trong `operations_loop` | Registry coi agent stale (ngoài scope phiên này) |
| Command expiry | Gateway record (`expires_at`, ttl_s lúc enqueue) | mặc định 300s | Không renew (theo thiết kế — expiry là hard deadline của toàn bộ command, không phải ownership) | `EXPIRED`, fail-closed |

## Invariant (Bước 2) — đã áp dụng
1-10 đều được implement: chỉ current attempt/token mới renew được visibility (Gateway
guard); chỉ đúng lease token mới renew được lease (Lua compare-and-expire); stale
attempt/token bị 409; terminal/expired không renew được (heartbeat guard); renewal
failure trả `False`/409, KHÔNG bao giờ coi là success; ownership loss trong lúc mutation
→ escalated (không tự COMPLETED); release chỉ với đúng token hiện tại (Lua compare-and-
delete); renewal luôn set lại full TTL (monotonic, không có nhánh giảm).

## Lease renewal protocol (Bước 2)
`src/aoip/agent/lease.py`: `renew(scope, token, ttl_s)` — Lua 1 round-trip
(`GET==token → SET EX ttl_s; else 0`). `release()` cũng thành Lua compare-and-delete
(trước đây GET-rồi-DEL rời, có race window nếu lease hết hạn + agent khác acquire GIỮA
hai lệnh). `refresh()` cũ giữ làm alias deprecated cho `renew()`.

## Visibility heartbeat protocol (Bước 4)
`POST /webhook/agent/rt/commands/heartbeat` — request fields giống `AckDelivered`
(`agent_id, tenant_id, command_id, delivery_attempt, fencing_token, expected_version?`).
Guard: 404 nếu record không tồn tại; 409 `terminal_no_heartbeat` nếu đã terminal; auto-
EXPIRE + 409 `expired` nếu `now >= expires_at`; 409 qua `_check_fencing` (agent_mismatch/
stale_delivery_attempt/invalid_fencing_token/version_conflict); 409 `not_running` nếu
state không phải RUNNING/RECONCILING. Thành công: gia hạn `visibility_deadline` +
zset score, bump `record_version`, **KHÔNG đổi `delivery_attempt`, KHÔNG cấp
`fencing_token` mới**, trả `{visibility_deadline, record_version}`.

## Renewal coordinator (Bước 5)
`src/aoip/agent/renewal.py::run_with_renewal(coro, renew_fn, interval_s)` — helper DÙNG
CHUNG, generic: chạy `coro` trong khi một task nền gọi `renew_fn()` mỗi `interval_s`.
`renew_fn` trả `False` (ownership_lost thật) hoặc raise (lỗi mạng thoáng qua, KHÔNG mất
ownership — log rồi tiếp tục). Task LUÔN bị cancel + await sau khi `coro` xong (kể cả
lỗi), CancelledError KHÔNG bị nuốt. Dùng ở 2 nơi:
- `operations.run_guarded_recovery`: `renew_fn = lease.renew(target, token, lease_ttl_s)`
  bọc `execute_recovery(...)`.
- `delivery_loop.DeliveryLoop._run_executor_with_heartbeat`: `renew_fn =
  client.heartbeat_visibility(...)` bọc `self._executor(entry.payload)`. Nếu client
  không có `heartbeat_visibility` (fake cũ/legacy) → bỏ qua im lặng, KHÔNG lỗi.

## Ownership loss semantics (Bước 6)
- **Trước mutation**: đã có sẵn (lease `acquire()` fail → `aborted`, KHÔNG chạy
  `execute_recovery`) — không đổi.
- **Trong/sau mutation** (lease renew fail trong lúc `execute_recovery` chạy): mutation
  KHÔNG bị huỷ giữa chừng (an toàn hơn để nó verify xong bằng physical-proof revalidation
  có sẵn). Nếu kết quả là `"recovered"` → ghi đè thành `"escalated"` +
  `EV_RECOVERY_OWNERSHIP_LOST` audit, reason
  `ownership_lost_during_mutation_ambiguous`. Nếu kết quả là `"aborted"` (không có side
  effect — vd đã healthy từ trước) → GIỮ NGUYÊN, mất ownership vô hại.
- **Sau verify, trước terminal report** (delivery visibility mất trong lúc RUNNING dài,
  Gateway đã redeliver attempt mới): `report_terminal` với attempt cũ → Gateway 409 →
  `HTTPOmniClient.report_terminal` KHÔNG raise, trả
  `{"acknowledged": False, "conflict": True, "error": ...}`; `DeliveryLoop._report_and_
  archive` log `terminal_report_conflict`, GIỮ outcome cục bộ (đã persist trước đó),
  KHÔNG archive — resume/tick sau tự re-report.

## Config (Bước 7)
`src/aoip/agent/timing_config.py::TimingConfig` — 5 field
(`execution_lease_ttl_s, lease_renewal_interval_s, gateway_visibility_s,
visibility_renewal_interval_s, action_timeout_s`), validate: mọi giá trị > 0;
`renewal_interval < ttl` (cả lease lẫn visibility); TTL/visibility ≥ 2x renewal interval
tương ứng (safety margin). `InvalidTimingConfig` raise fail-closed nếu vi phạm. Đây là
dataclass tham chiếu/validate — CHƯA wiring vào daemon CLI (gap: xem "Remaining gaps"
nếu operator muốn override qua env, cần thêm bước load từ `AOIP_LEASE_*`/`AOIP_VISIBILITY_*`).

## Failure semantics (Bước 8, bảng)

| Failure point | Side effect possible | Final behavior |
|---|---:|---|
| Lease acquire fail (đã bị giữ) | Không | `aborted`, `lease_denied` |
| Lease renew fail, outcome=recovered | Có (mutation đã chạy) | `escalated`, `ownership_lost_during_mutation_ambiguous` |
| Lease renew fail, outcome=aborted | Không | Giữ nguyên `aborted`, không escalate |
| Lease renew transient network error | N/A | Log, KHÔNG mark lost, retry lần sau |
| Visibility heartbeat 409 trong lúc RUNNING | Có thể | Redelivery tạo attempt mới; report_terminal cuối cùng của attempt cũ bị 409 conflict → giữ outcome cục bộ, không archive |
| report_terminal 409 (stale attempt) | Đã persist local | KHÔNG raise, KHÔNG crash tick(), retry ở resume sau |
| Agent crash giữa RUNNING | Không renew nữa | Lease + visibility tự hết hạn theo TTL; redelivery attempt mới; attempt cũ không report được vào attempt mới (fencing) |
| Gateway heartbeat fail, lease renew vẫn OK | Không | Redelivery có thể xảy ra (nguy cơ 2 agent cùng chạy); execution lease vẫn chặn mutation kép ở tầng agent |
| Cả lease renew và Gateway heartbeat đều fail | Có thể | Ambiguous → escalate ở tầng execution; delivery tầng report có thể conflict — KHÔNG generic retry vô hạn, đều có giới hạn rõ (lease: escalate 1 lần; delivery: giữ outcome chờ resume) |

## Implementation summary (Bước 5 deliverable)
- `src/aoip/agent/lease.py`: `_RENEW_SCRIPT`/`_RELEASE_SCRIPT` (Lua atomic), `renew()` mới,
  `refresh()` → alias deprecated, `release()` chuyển sang Lua.
- `src/aoip/agent/renewal.py` (MỚI): `run_with_renewal()`, `RenewalOutcome`.
- `src/aoip/agent/timing_config.py` (MỚI): `TimingConfig`, `InvalidTimingConfig`.
- `src/aoip/agent/operations.py`: `run_guarded_recovery` thêm `lease_renewal_interval_s`
  param, bọc `execute_recovery(...)` bằng `run_with_renewal`, map ownership-lost→escalated.
- `src/aoip/audit.py`: thêm `EV_RECOVERY_OWNERSHIP_LOST`.
- `src/gateway/routes/agent_runtime.py`: route mới `POST /commands/heartbeat`
  (`heartbeat_command`), docstring cập nhật (visibility vs lease).
- `src/aoip/agent/omni_client.py`: `HTTPOmniClient.heartbeat_visibility()` mới;
  `report_terminal()` không còn `raise_for_status()` mù — 409 → trả dict conflict.
- `src/aoip/agent/delivery_loop.py`: `DeliveryLoop.__init__` thêm `heartbeat_interval_s`;
  `_run_executor_with_heartbeat()` mới (bọc executor bằng `run_with_renewal`, bỏ qua nếu
  client thiếu `heartbeat_visibility`); `_report_and_archive` xử lý `ack["conflict"]`.
- Tests: `tests/test_aoip_lease_renewal.py` (MỚI, 9 test); `tests/test_gateway_agent_
  runtime.py` (+7 test heartbeat); `tests/test_aoip_operations.py` (+3 test ownership-loss/
  long-running); `tests/test_aoip_delivery_loop.py` (+4 test heartbeat/conflict).

## Concurrency/failure evidence (Bước 6 deliverable)
- `test_renew_atomic_under_race_old_owner_vs_new_owner` — renew cũ KHÔNG ghi đè lease mới.
- `test_long_running_execution_renews_lease_no_redelivery` — mutation chạy lâu hơn TTL
  gốc, renewal giữ lease sống, `recovered` bình thường.
- `test_ownership_lost_during_mutation_becomes_escalated_not_completed` — lease bị giành
  giữa mutation → escalated, KHÔNG COMPLETED giả.
- `test_ownership_lost_but_already_healthy_stays_completed_no_action` — mất ownership vô
  hại khi không có side effect.
- `test_heartbeat_fires_during_long_running_executor` — heartbeat thật sự gọi nhiều lần.
- `test_no_orphan_asyncio_task_after_tick_with_heartbeat` — không orphan task.
- `test_report_terminal_conflict_keeps_local_outcome_no_archive` — 409 không crash, giữ
  outcome, không archive.
- `test_run_with_renewal_cancellation_not_swallowed` — CancelledError không bị nuốt.

## Test results (Bước 7 deliverable)

| Command | Result | Notes |
|---|---|---|
| `pytest tests/test_aoip_lease_renewal.py -q` | 9 passed | Lease atomic renew/release, coordinator |
| `pytest tests/test_gateway_agent_runtime.py -q` | 21 passed | 14 cũ + 7 heartbeat mới |
| `pytest tests/test_aoip_delivery_loop.py -q` | 8 passed | 4 cũ + 4 heartbeat/conflict mới |
| `pytest tests/test_aoip_operations.py -q` | 25 passed | 22 cũ + 3 ownership-loss/long-running |
| `pytest tests/ -q -k "aoip or gateway_agent_runtime"` | 232 passed | Toàn bộ AOIP+Gateway runtime |
| `pytest tests/ -q --ignore=tests/integration` (full suite) | 5819 passed, 1 failed | Fail = `test_register_then_real_system_metrics_emitted_through_real_pipeline`, xác nhận pre-existing bằng `git stash` (không liên quan session này/trước) |
| Lint/type-check riêng | Not run | Không có pyflakes/mypy sẵn trong `.venv`; đã `ast.parse` các file sửa |

## Remaining gaps (Bước 8/9 deliverable — không mở rộng scope)
- **Shared domain transition enforcement**: `_advance`/`heartbeat_command`/`terminal_command`
  mới guard attempt/token/version cho CHÍNH các route này; chưa có validate expected-
  current-state tổng quát cho MỌI transition path khác trong hệ thống (nếu có nơi khác).
- **Payload integrity/signing**: `payload_hash` field tồn tại trong record nhưng KHÔNG
  được verify — chưa có hash/signature check thật.
- **First typed capability certification**: chưa có (ngoài scope timing này, brief liệt
  kê như remaining gap tổng thể của Living Operations Runtime).
- `TimingConfig` chưa wiring vào daemon CLI/env loader (chỉ là dataclass validate sẵn
  sàng dùng) — nếu operator cần override TTL/interval qua env, cần thêm bước load.
- Structured metrics `execution_lease_renew_success/failed` chưa tách riêng khỏi log
  chung `renew_error`/`ownership_lost` (đã có structured log, chưa có counter riêng biệt
  theo tên chính xác brief liệt kê — log hiện tại đủ để trace nhưng chưa named-metric).

## Không tuyên bố absolute exactly-once
Effectively-once vẫn đúng như trước: renewal giảm XÁC SUẤT redelivery/ownership-loss khi
mutation dài, KHÔNG loại bỏ nó tuyệt đối (network partition dài hơn mọi TTL vẫn có thể
xảy ra) — ambiguous case luôn resolve về escalate/giữ-outcome-chờ-reconcile, KHÔNG bao giờ
tự nhận thành công khi không chắc.

## Branch và commit
`feature/living-operations-runtime` @ `755220f` (HEAD trước phiên này). Phiên này CHƯA commit.

## Files chính đã thay đổi
`src/aoip/agent/lease.py`, `src/aoip/agent/renewal.py` (mới),
`src/aoip/agent/timing_config.py` (mới), `src/aoip/agent/operations.py`, `src/aoip/audit.py`,
`src/gateway/routes/agent_runtime.py`, `src/aoip/agent/omni_client.py`,
`src/aoip/agent/delivery_loop.py`, `tests/test_aoip_lease_renewal.py` (mới),
`tests/test_gateway_agent_runtime.py`, `tests/test_aoip_operations.py`,
`tests/test_aoip_delivery_loop.py`.

## Quyết định đã chốt
- KHÔNG dùng RecoveryOutcome status mới ("ambiguous"/"reconciling") — tái dùng
  `"escalated"` (đã tồn tại, đã là domain "cần người xử lý") thay vì mở rộng domain state
  machine, đúng ràng buộc "không shared domain transition refactor lớn".
- `run_with_renewal` là helper DÙNG CHUNG cho cả lease renewal (execution layer) và
  visibility heartbeat (delivery layer) — cùng một shape (renew_fn + interval quanh một
  coroutine), tránh viết 2 coordinator riêng biệt.
- Heartbeat coordinator ở `DeliveryLoop` dùng `getattr(client, "heartbeat_visibility",
  None)` để bỏ qua im lặng nếu client không hỗ trợ — giữ backward-compat với
  fake/legacy client trong test hiện có, KHÔNG bắt buộc mọi client implement ngay.
- `report_terminal` đổi hành vi: 409 KHÔNG còn raise (trước đây `_post_rt` raise_for_status
  mù sẽ crash tick() nếu Gateway từ chối) — đây là thay đổi CẦN THIẾT để chịu được fencing
  409 xảy ra tự nhiên hơn khi có redelivery-trong-lúc-running.
- KHÔNG sửa Gateway atomic claim protocol ngoài heartbeat guard — `_CLAIM_SCRIPT` không đổi.
- KHÔNG redesign execution lease abstraction — chỉ thêm `renew()` atomic, giữ nguyên
  `acquire()`/`holder_token()`.

## Verification đã chạy
- `pytest tests/test_aoip_lease_renewal.py tests/test_gateway_agent_runtime.py tests/test_aoip_delivery_loop.py tests/test_aoip_operations.py -q` → 63 passed.
- `pytest tests/ -q -k "aoip or gateway_agent_runtime"` → 232 passed.
- `pytest tests/ -q --ignore=tests/integration` → 5819 passed, 1 failed (pre-existing,
  không liên quan, xác nhận bằng git stash).

## Deployment hiện tại
Không đổi. Route mới `/commands/heartbeat` chưa được gọi bởi CLI production (daemon.main()
chưa wiring `heartbeat_interval_s`/`TimingConfig` — DeliveryLoop mặc định 15s nếu được dùng
qua `run_daemon()` trực tiếp với HTTPOmniClient thật, do `heartbeat_visibility` đã tồn tại
trên `HTTPOmniClient`).

## Blockers
None.

## Next step chính xác
Theo brief gốc, còn lại (không blocker, đã ghi rõ ở "Remaining gaps"):
shared domain transition enforcement tổng quát; payload integrity/signing; typed capability
certification; wiring `TimingConfig` vào daemon CLI/env nếu operator cần override.

## Lệnh cần chạy lại
- `.venv/bin/python -m pytest tests/test_aoip_lease_renewal.py -q`
- `.venv/bin/python -m pytest tests/ -q -k "aoip or gateway_agent_runtime"`
- `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration`

## Không được làm lại
- KHÔNG thêm RecoveryOutcome status mới — dùng `escalated` sẵn có cho ambiguous.
- KHÔNG đổi `_CLAIM_SCRIPT`/atomic claim protocol (heartbeat là route RIÊNG, không chạm claim).
- KHÔNG viết lại `run_with_renewal` thành 2 bản riêng cho lease/visibility — giữ 1 helper chung.
- KHÔNG bắt buộc mọi `RuntimeDeliveryClient` phải implement `heartbeat_visibility` — optional,
  `getattr` fallback là chủ ý.

## Tài liệu liên quan
- `src/aoip/agent/lease.py`, `src/aoip/agent/renewal.py`, `src/aoip/agent/timing_config.py`
  (docstring module đầy đủ protocol).
- `src/gateway/routes/agent_runtime.py` (docstring heartbeat section mới).
- `src/aoip/agent/delivery_loop.py` (docstring long-running execution safety mới).
- `tests/test_aoip_lease_renewal.py`, `tests/test_gateway_agent_runtime.py` (heartbeat
  section), `tests/test_aoip_operations.py` (ownership-loss section).
