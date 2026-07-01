# Current Session Handoff

## Deliverable hiện tại
Atomic delivery claim + delivery ownership/fencing cho Gateway Agent Runtime
(`src/gateway/routes/agent_runtime.py`): claim không còn GET-rồi-SET rời (race), mỗi claim
là một Lua round-trip; mọi request sau delivery (accept/progress/terminal) phải khớp
`delivery_attempt`/`fencing_token` hiện tại của record, sai → 409 fail-closed.

## Definition of Done
`QUEUED → atomic claim (Lua) → DELIVERED (attempt+token mới) → Agent ACK có
attempt/token hợp lệ → transition được bảo vệ`. Hai poller cùng agent/tenant chỉ một bên
thắng một attempt. Redelivery sau visibility timeout sinh attempt+token MỚI; attempt cũ bị
từ chối ở mọi endpoint (accept/progress/terminal). Stale tenant/agent/token/version đều bị
từ chối rõ ràng (409 + domain reason), KHÔNG silent accept. ✅ Xong — 209/209 test AOIP+gateway
pass (14 test gateway agent-runtime, trong đó 9 test mới cho concurrency/fencing); full suite
5796/5797 pass (1 fail pre-existing, không liên quan — xác nhận bằng `git stash`).

## Trạng thái hiện tại
Hoàn thành, CHƯA commit.

## Đã hoàn thành
- `src/gateway/routes/agent_runtime.py`:
  - `_CLAIM_SCRIPT` (Lua, dùng `cjson`) — atomic claim 1 round-trip/command: kiểm record tồn
    tại, chưa terminal, chưa expired (fail-closed EXPIRED ngay trong script nếu hết hạn), state
    claimable (`QUEUED` hoặc `DELIVERED` với `visibility_deadline` đã qua), rồi tăng
    `delivery_attempt`, sinh `fencing_token = "{command_id}:{attempt}"` (duy nhất theo attempt,
    không cần random vì attempt đơn điệu), set `DELIVERED` + `visibility_deadline` mới,
    `record_version += 1`.
  - `poll_commands` gọi `_claim()` (wrapper `redis.eval`) cho từng candidate `due` thay vì
    GET/SET rời — loại bỏ race window cũ.
  - Record schema thêm: `delivery_attempt`, `fencing_token`, `delivered_at`,
    `visibility_deadline`, `record_version` (giữ `delivery_count`/`last_delivered_at` cũ làm
    alias tương thích, không xoá).
  - `AckDelivered` (Pydantic) thêm field bắt buộc `delivery_attempt: int`, `fencing_token: str`,
    optional `expected_version: int | None` — áp dụng cho accept/progress/terminal (Terminal kế
    thừa AckDelivered).
  - `_check_fencing()` + `_OwnershipConflict` — guard agent_id → delivery_attempt →
    fencing_token → record_version, raise domain reason (`agent_mismatch`,
    `stale_delivery_attempt`, `invalid_fencing_token`, `version_conflict`).
  - `_advance()` (accept/progress) ownership-guarded, idempotent nếu record ĐÃ ở target với
    đúng attempt/token (không bump version thêm).
  - `terminal_command()`: nếu đã terminal — cùng state+outcome → idempotent True; khác outcome
    → 409 `terminal_outcome_conflict` (không ghi đè); nếu chưa terminal — fencing check trước
    khi ghi.
  - Structured log (`logging.getLogger(__name__)`, KHÔNG log payload/secret):
    `claim_success`, `redelivery`, `claim_conflict`, `expired_claim_rejected`,
    `ownership_conflict` (kèm reason).
- `src/aoip/agent/inbox.py`: `InboxEntry` thêm `delivery_attempt`/`fencing_token`/
  `record_version` (default 0/""/0, backward-compat); `LocalInbox.persist()` nhận 3 field này,
  giữ nguyên bất biến "đã có (redelivery) → trả entry hiện tại, KHÔNG reset".
- `src/aoip/agent/delivery_loop.py`: `RuntimeDeliveryClient` Protocol + `DeliveryLoop.tick()`/
  `_report_and_archive()` echo `delivery_attempt`/`fencing_token` từ `InboxEntry` vào MỌI lời
  gọi accept/progress/report_terminal.
- `src/aoip/agent/omni_client.py`: `HTTPOmniClient.accept/progress/report_terminal` nhận + gửi
  `delivery_attempt`/`fencing_token` trong request body.
- Tests: `tests/test_gateway_agent_runtime.py` viết lại — 14 test (5 cũ cập nhật fencing +
  9 mới: 2 concurrent pollers chỉ 1 thắng, redelivery bump attempt/token, stale-attempt bị
  reject ở cả 3 endpoint, invalid token, version conflict, idempotent retry, terminal-outcome-
  conflict, tenant mismatch → 404, agent mismatch → 409). `tests/test_aoip_delivery_loop.py`,
  `tests/test_aoip_agent_daemon.py`, `tests/test_aoip_operations.py`,
  `tests/test_aoip_runtime_config.py`: fake client cập nhật `**kwargs` để nhận
  attempt/token (không đổi test intent).
- Verified: `pytest tests/ -q -k "aoip or gateway_agent_runtime"` → 209 passed;
  `pytest tests/ -q --ignore=tests/integration` → 5796/5797 passed (1 fail pre-existing,
  `test_register_then_real_system_metrics_emitted_through_real_pipeline`, xác nhận bằng
  `git stash` chạy trước khi có thay đổi phiên này — KHÔNG liên quan).

## Branch và commit
`feature/living-operations-runtime` @ `36f1ea8` (HEAD trước phiên này). Phiên này CHƯA commit.

## Files chính đã thay đổi
`src/gateway/routes/agent_runtime.py`, `src/aoip/agent/inbox.py`,
`src/aoip/agent/delivery_loop.py`, `src/aoip/agent/omni_client.py`,
`tests/test_gateway_agent_runtime.py`, `tests/test_aoip_delivery_loop.py`,
`tests/test_aoip_agent_daemon.py`, `tests/test_aoip_operations.py`,
`tests/test_aoip_runtime_config.py`.

## Quyết định đã chốt
- `fencing_token = f"{command_id}:{delivery_attempt}"` — KHÔNG dùng random/UUID. Attempt
  đơn điệu (tăng trong CÙNG Lua script với state transition) đã đảm bảo duy nhất tuyệt đối
  theo attempt, không cần thêm nguồn entropy.
- Fencing áp dụng cho TOÀN BỘ `/rt/` channel (không có "legacy path bỏ qua fencing") — kênh
  này VỐN LÀ mutating recovery channel (docstring module đã ghi rõ), nên không cần cửa sổ
  tương thích ngược; agent client + gateway route sửa trong CÙNG một commit.
- `_check_fencing` thứ tự kiểm: agent_id → attempt → token → version. Thứ tự chỉ ảnh hưởng
  `reason` trả về trong response, không ảnh hưởng an toàn (mọi nhánh đều reject).
- Idempotent retry (cùng attempt/token/version, record ĐÃ ở target) → trả thành công, KHÔNG
  bump `record_version` thêm lần nữa — tránh version trôi vô hạn từ retry vô hại.
- Terminal report khác outcome sau khi đã terminal → 409 `terminal_outcome_conflict`, KHÔNG
  ghi đè — khác hành vi cũ (trước đây im lặng trả `idempotent: true` mà không so outcome).
- KHÔNG sửa `aoip.agent.operations.py`/recovery executor/runtime mode — đúng scope boundary.
- KHÔNG sửa lease renewal — gap còn lại: nếu agent RUNNING lâu hơn `_VISIBILITY_S` (60s),
  Gateway redeliver attempt mới trong khi agent cũ vẫn đang chạy → report cuối cùng của agent
  cũ dùng attempt cũ → bị 409 `stale_delivery_attempt`. Đây là hệ quả CHỦ Ý của việc không làm
  lease renewal trong phiên này (đã ghi trong docstring `delivery_loop.py`), KHÔNG phải bug mới.

## Verification đã chạy
- `pytest tests/test_gateway_agent_runtime.py -q` → 14 passed.
- `pytest tests/ -q -k "aoip or gateway_agent_runtime"` → 209 passed.
- `pytest tests/ -q --ignore=tests/integration` → 5796 passed, 1 failed (pre-existing,
  không liên quan — xác nhận bằng git stash), 5 deselected.
- Lint/type-check riêng: KHÔNG chạy (không có pyflakes/mypy cấu hình sẵn trong `.venv`); đã
  kiểm `ast.parse` cho file sửa — không lỗi cú pháp.
- `systemd-analyze`: N/A (không đụng deployment phiên này).

## Deployment hiện tại
Không đổi. Chưa deploy protocol mới lên VM/K8s nào — thay đổi chỉ ở code + test.

## Blockers
None.

## Next step chính xác
Theo brief gốc, các gap còn lại (KHÔNG blocker an toàn — lease+ledger vẫn backstop mutation):
- **Lease renewal**: agent RUNNING lâu hơn visibility deadline (60s) cần cơ chế gia hạn
  (heartbeat/renew endpoint) để tránh redelivery-khi-vẫn-đang-chạy (xem "Quyết định đã chốt").
- **Shared domain transition enforcement**: `_advance`/`terminal_command` mới guard
  attempt/token/version, CHƯA validate expected-current-state tổng quát cho mọi transition
  path ngoài các route này (nếu có).
- **Payload integrity**: `payload_hash` field tồn tại trong record nhưng KHÔNG được verify —
  chưa có hash/signature check.

## Lệnh cần chạy lại
- `.venv/bin/python -m pytest tests/test_gateway_agent_runtime.py -q`
- `.venv/bin/python -m pytest tests/ -q -k "aoip or gateway_agent_runtime"`
- `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration`

## Không được làm lại
- KHÔNG đổi `_CLAIM_SCRIPT` để random fencing_token — attempt-based token đã đủ, đừng thêm
  entropy không cần thiết.
- KHÔNG nới lỏng fencing thành "optional/legacy-compatible" trên `/rt/` channel — kênh này
  luôn là mutating, không có lý do hợp lệ để bỏ qua attempt/token.
- KHÔNG làm lease renewal/visibility-extension trong slice tiếp theo trừ khi được yêu cầu rõ
  — đó là next step (a) riêng, cần thiết kế heartbeat/renew endpoint mới.
- KHÔNG sửa `aoip.agent.operations.py`/recovery executor logic/runtime config modes.

## Tài liệu liên quan
- `src/gateway/routes/agent_runtime.py` (module docstring đã cập nhật đầy đủ protocol mới).
- `src/aoip/agent/delivery_loop.py` (module docstring ghi rõ gap redelivery-while-running).
- `tests/test_gateway_agent_runtime.py` (test suite atomic claim + fencing, 14 test).
