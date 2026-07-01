# Current Session Handoff

## Deliverable hiện tại
Production runtime wiring: đảm bảo AOIP Agent CLI/systemd entrypoint (`daemon.main()`) KHÔNG
bao giờ âm thầm chạy no-op khi operator tin mutation đang hoạt động. Buộc chọn RÕ runtime mode.

## Definition of Done
`AOIP_AGENT_MODE` = `observe_only` (mặc định, fail-safe) hoặc `mutation_enabled`.
`mutation_enabled` build ĐẦY ĐỦ redis/audit_log/gate/transport/recovery-executor; thiếu/lỗi
bất kỳ dependency nào → startup FAIL (raise `AgentBootstrapError`, exit non-zero), KHÔNG fallback
no-op ngầm. `observe_only` executor không bao giờ trả COMPLETED cho mutating command (ESCALATED,
reason=`executor_disabled_observe_only`). ✅ Xong — 195/195 test AOIP pass (+8 mới).

## Trạng thái hiện tại
Hoàn thành, CHƯA commit. Working tree có thay đổi (xem `git status`).

## Đã hoàn thành
- **Mới**: `src/aoip/agent/runtime_config.py` — `MODE_OBSERVE_ONLY`/`MODE_MUTATION_ENABLED`,
  `RuntimeStatus` (executor_mode/executor_status: ACTIVE|DISABLED), `AgentBootstrapError`,
  `observe_only_executor`, `build_agent_runtime(mode, agent_id, env)`. Đọc env bắt buộc:
  `AOIP_REDIS_URL`, `AOIP_AUDIT_LOG_PATH`, `AOIP_GATE_ALLOWED_FAILURE_MODES`,
  `AOIP_GATE_ALLOWED_SUBSTRATES`, `AOIP_GATE_SCOPE_PREFIX`, `AOIP_GATE_MAX_RISK`,
  `AOIP_GATE_MIN_DIAGNOSIS_CONFIDENCE`, `AOIP_GATE_MAX_DIAGNOSIS_AGE_S`; optional
  `AOIP_RECOVERY_SSH_HOST`/`AOIP_RECOVERY_SSH_USER`/`AOIP_RECOVERY_TARGET`,
  `AOIP_AUTO_EXECUTE_ENABLED` (default false).
- `daemon.py`: `main()` đọc `AOIP_AGENT_MODE` (default `observe_only`), gọi
  `build_agent_runtime()`, in structured status log (`[bootstrap] executor_mode=... executor_status=...`),
  tiêm executor tường minh vào `run_daemon()`. `_noop_executor`/`_default_executor` chỉ còn dùng
  khi gọi `run_daemon()` trực tiếp không qua `main()` (test/dev injection, KHÔNG phải CLI path).
  Docstring module cập nhật để không còn mô tả sai (trước đây tuyên bố adapter là default nhưng
  `main()` chưa từng tiêm dependency — CLI luôn rơi `_noop_executor` một cách IM LẶNG).
- `deploy/systemd/aoip-agent.service`: thêm `Environment=AOIP_AGENT_MODE=observe_only` (fail-safe
  default, override được bởi `EnvironmentFile=/etc/aoip/agent.env` nạp sau).
- `tests/test_aoip_runtime_config.py` (mới, 8 test): mutation_enabled đủ dep → ACTIVE; thiếu
  từng dependency (redis/audit_log/gate/numeric-invalid) → `AgentBootstrapError` fail-closed;
  mode không hợp lệ → fail-closed; observe_only → DISABLED, không cần dependency; observe_only
  executor không bao giờ COMPLETED end-to-end qua `DeliveryLoop` thật.
- Verified: `pytest tests/ -q -k aoip` → 195 passed (187 cũ + 8 mới).

## Branch và commit
`feature/living-operations-runtime` @ `d3c27e5` (HEAD trước phiên này). Phiên này CHƯA commit.

## Working tree
Có thay đổi: `src/aoip/agent/runtime_config.py` (mới), `src/aoip/agent/daemon.py`,
`deploy/systemd/aoip-agent.service`, `tests/test_aoip_runtime_config.py` (mới),
`docs/handoffs/CURRENT_SESSION.md`.

## Files chính đã thay đổi
`src/aoip/agent/runtime_config.py`, `src/aoip/agent/daemon.py`,
`deploy/systemd/aoip-agent.service`, `tests/test_aoip_runtime_config.py`.

## Quyết định đã chốt
- Mode mặc định khi `AOIP_AGENT_MODE` không set = `observe_only` (fail-safe: không mutation
  ngoài ý muốn operator). Muốn mutation thật PHẢI set rõ `mutation_enabled` + đủ config.
- KHÔNG suy ra observe-only từ lỗi dependency — `mutation_enabled` thiếu dependency là LỖI
  (raise), không phải tín hiệu để tự chuyển mode.
- Exception dependency-construction KHÔNG bị broad-catch trong `runtime_config.py` — chỉ bọc
  lại thành `AgentBootstrapError` có tên field thiếu rõ ràng, rồi để propagate lên `main()`
  (không catch ở đó) → Python traceback mặc định + exit non-zero, đúng systemd `Restart=always`
  policy hiện có (không đổi).
- `run_daemon()` giữ nguyên chữ ký cũ (`redis=`, `transport=`, ... optional) để không phá test
  hiện có dùng inject trực tiếp — thay đổi CHỈ ở `main()`.
- KHÔNG sửa Gateway atomic claim, lease renewal algorithm, payload hash/signing, K8s manifest,
  Portal — đúng scope boundary đã cho.

## Verification đã chạy
- `.venv/bin/python -m pytest tests/test_aoip_runtime_config.py tests/test_aoip_operations.py tests/test_aoip_agent_daemon.py -q` → 31 passed.
- `.venv/bin/python -m pytest tests/ -q -k aoip` → 195 passed.
- Import sanity: `PYTHONPATH=src python -c "from aoip.agent.daemon import main; ..."` → OK.
- `systemd-analyze verify` → Not run (macOS, không có systemd).
- Full suite / lint ngoài phạm vi AOIP: KHÔNG chạy phiên này.

## Deployment hiện tại
N/A — không deploy gì trong phiên này. Pod `omni-fullstack` không liên quan tới AOIP agent VM.
Operator cần set `AOIP_AGENT_MODE=mutation_enabled` + `AOIP_REDIS_URL`/`AOIP_AUDIT_LOG_PATH`/
`AOIP_GATE_*` trong `/etc/aoip/agent.env` để bật mutation thật trên VM — CHƯA làm trong phiên này.

## Blockers
None.

## Next step chính xác
Chọn 1 trong 2 (theo brief gốc, KHÔNG blocker mutation-safety, là hardening):
(a) atomic delivery claim (Lua script cho `poll_commands` trong
`src/gateway/routes/agent_runtime.py`, thay 4-op rời bằng 1 op atomic); hoặc
(b) Gateway transition enforcement (`_advance` cần validate expected-current-state).
Còn lại các gap đã biết: lease renewal, payload integrity (hash/signing) — KHÔNG đụng tới.

## Lệnh cần chạy lại
- `.venv/bin/python -m pytest tests/test_aoip_runtime_config.py tests/test_aoip_agent_daemon.py tests/test_aoip_operations.py -q`
- `.venv/bin/python -m pytest tests/ -q -k aoip`
- `bash tests/claude_hooks/test_session_hooks.sh`

## Không được làm lại
- KHÔNG viết lại `runtime_config.build_agent_runtime`/`observe_only_executor` — đã xong, 8 test.
- KHÔNG thêm mode thứ 3 ngoài `observe_only`/`mutation_enabled` trừ khi được yêu cầu rõ.
- KHÔNG đổi `run_daemon()` signature test-injection path (`redis=`/`transport=`/...) — giữ
  backward-compat cho `tests/test_aoip_operations.py` dòng ~387-415.
- KHÔNG atomic-claim/transition-enforcement TRƯỚC khi đọc lại code hiện tại của
  `src/gateway/routes/agent_runtime.py` — đọc trước khi sửa.

## Tài liệu liên quan
- `src/aoip/agent/runtime_config.py` (mode bootstrap mới).
- `src/aoip/agent/daemon.py` (main() wiring mode → executor).
- `deploy/systemd/aoip-agent.service` (fail-safe default env).
- `tests/test_aoip_runtime_config.py` (test suite runtime mode).
- `src/gateway/routes/agent_runtime.py` (twin phía Gateway — mục tiêu next step, KHÔNG đụng).
