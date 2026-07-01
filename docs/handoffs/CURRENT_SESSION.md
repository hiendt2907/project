# Current Session Handoff

## Deliverable hiện tại
**M1 — Human-approved Systemd Service Recovery**: vertical slice sản phẩm đầu tiên trên
Living Operations Runtime. Chuyển từ infrastructure hardening (5 phiên trước: durable
delivery, atomic claim/fencing, lease renewal/heartbeat) sang capability THẬT khách hàng
dùng được: `systemd.restart_unit`.

## Product journey (Bước 1 deliverable)
```
Incident (evidence: Finding "svc DOWN") → Decision (aoip.decision.decide_recovery, ĐÃ CÓ)
→ typed capability payload (build_typed_payload) → approval binding (issue_capability_command
  — hash payload TẠI THỜI ĐIỂM approve) → enqueue Gateway durable command (ĐÃ CÓ, không đổi)
→ Agent poll (atomic claim + fencing, ĐÃ CÓ) → preflight (capability/version, unit regex,
  payload-hash, allowlist, unit-exists) → run_guarded_recovery THẬT (lease/ledger/fencing,
  ĐÃ CÓ, không bypass) → verify (systemctl is-active, ĐÃ CÓ trong recovery.py) → structured
  product outcome → Gateway terminal report → audit hash-chain đầy đủ correlation IDs.
```

## Bước 1 — Flow hiện có trước phiên này (file:symbol, trạng thái)
- Mission/orchestrator: `src/aoip/mission.py` — **implemented** (`Mission`, `MissionState`,
  `run_mission()`; không có class `MissionOrchestrator` riêng, `run_mission()` là driver).
- Decision: `src/aoip/decision.py::decide_recovery()`, `src/aoip/objects.py:88 Decision` —
  **implemented**, tái dùng nguyên vẹn (KHÔNG sửa).
- Approval/HITL: `src/aoip/recovery.py:111 Approval` + `.issue()` — **implemented**, tái dùng.
  Legacy HITL stack (`src/services/playbook/`, `gateway/routes/{playbooks,autonomy}.py`) —
  **cố ý KHÔNG coupling**, khác domain.
- RecoveryGate: `src/aoip/recovery.py:98` — **implemented**, KHÔNG sửa (allowlist đặt ở
  capability layer, không đụng gate chung).
- `run_guarded_recovery`/`execute_recovery`: `src/aoip/agent/operations.py:51`,
  `src/aoip/recovery.py:225` — **implemented**, tái dùng nguyên vẹn (lease renewal đã có từ
  phiên trước, không sửa thêm).
- Systemd operator: `src/aoip/recovery.py:36-90` (`_sd_is_broken/_sd_capture/_sd_apply/_sd_health`,
  `OPERATORS[("process_down","systemd")]`) — **implemented**, KHÔNG unit validation (gap đã vá).
- Unit allowlist: **absent trước phiên này** — MỚI xây (`SystemdRestartPolicy`).
- Verification: `recovery.py:281-310` (health + dependents) — **implemented**, tái dùng.
- Audit: `src/aoip/audit.py` 10 `EV_*` — **implemented**, tái dùng nguyên vẹn.
- `decode_recovery_command`/`_map_recovery_outcome`: `operations.py:244,289` — **implemented**
  nhưng KHÔNG đủ cho product outcome taxonomy → capability layer có `_decode`/
  `_classify_product_outcome` RIÊNG (không sửa bản chung, vì bản chung phục vụ generic
  recovery command, không riêng capability này).
- CLI/portal approval-issuing: **absent** — MỚI xây (`aoip.console.approve_systemd_restart`).

## Capability contract (Bước 2 deliverable)
`src/aoip/capabilities/systemd_restart.py::CAPABILITY_NAME = "systemd.restart_unit"`,
`CAPABILITY_VERSION = "1"`. Payload (`build_typed_payload`): `capability, capability_version,
target.unit, reason.{mission_id,decision_id,incident_id,summary}, preconditions.
{require_unit_exists,require_allowlisted}, verification.{require_active_state,health_check}`
— ĐÚNG schema Bước 2, KHÔNG raw shell command. `CAPABILITY_METADATA`: requires_approval=True,
risk_class="low", blast_radius="single_unit", reversibility="reversible_via_restart",
verification_required=True. Registry tra cứu: `describe_capability(name, version)` — trả
None (fail-closed) nếu KHÔNG khớp CHÍNH XÁC name+version (KHÔNG generic fallback).

## Approval binding (Bước 3 deliverable)
`capability_payload_hash(typed_payload)` — sha256 canonical JSON (sorted keys) của
`{capability, capability_version, target, reason, preconditions, verification}`.
`issue_capability_command()` tính hash TẠI THỜI ĐIỂM approve, nhúng `approved_payload_hash`
vào envelope. `_decode()` (agent-side) RECOMPUTE hash từ payload nhận được, so sánh — khác
BẤT KỲ field nào (kể cả `reason.summary`) → `PRECONDITION_FAILED: payload_hash_mismatch`,
ZERO mutation. Đây là canonical-hash binding tối thiểu theo đúng yêu cầu Bước 6 (KHÔNG
PKI/signed envelope).

## Agent-side allowlist (Bước 4)
`SystemdRestartPolicy(allowed_units: frozenset, allow_self_restart=False,
agent_service_name="")`. `load_policy_from_env()` đọc `AOIP_ALLOWED_SYSTEMD_UNITS` (CSV),
`AOIP_ALLOW_SELF_RESTART`, `AOIP_AGENT_SERVICE_NAME`. Rỗng/thiếu env → allowlist RỖNG
(fail-closed, KHÔNG wildcard-allow). `validate_unit_name()` — regex
`^[A-Za-z0-9_.:@-]{1,128}\.service$`, reject path/whitespace/shell metachar/thiếu suffix.
Self-restart agent's own unit bị chặn mặc định trừ khi `allow_self_restart=True`.

## Preflight (Bước 5)
`build_systemd_restart_executor`'s inner `executor()` chạy TRƯỚC `run_guarded_recovery`:
capability/version (registry lookup) → unit name valid → payload_hash match → approval
approved+not-expired+tenant/decision-bound (qua `Approval.issue()` — reuse nguyên vẹn) →
unit allowlisted → unit exists (`systemctl show -p LoadState`, argv cố định, KHÔNG shell=True/
bash -c/eval). Ownership/lease (điểm 4-5 Bước 5) + agent mode (điểm 9) + conflicting-op
(điểm 10) ĐÃ được enforce ở tầng dưới (Gateway fencing, `run_guarded_recovery`'s
`lease.acquire()`) — KHÔNG re-implement, chỉ note evidence.

## Execution/Verification (Bước 7/8)
Reuse `_sd_apply` (argv `["sudo","systemctl","restart",unit]`, timeout 30s — KHÔNG shell)
qua `run_guarded_recovery`/`execute_recovery` nguyên vẹn (lease renewal + fencing từ 2 phiên
trước, KHÔNG sửa thêm). Verify: `_sd_health` (`systemctl is-active`) + dependents — KHÔNG
COMPLETED chỉ vì exit code 0 (execute_recovery LUÔN verify riêng sau execute).

## Structured product outcome (Bước 9)
10 outcome constants (`OUTCOME_EXECUTED_AND_VERIFIED` … `OUTCOME_UNSUPPORTED_CAPABILITY`)
+ `OUTCOME_SHADOW_RECOMMENDATION`. `_classify_product_outcome()` map RecoveryOutcome kỹ
thuật → nhãn sản phẩm. `_operator_summary()` — result operator-facing: attempted, target,
approver, evidence (capability_checks + recovery_evidence), duration_s, `next_step` gợi ý
hành động tiếp — KHÔNG raw traceback.

## Shadow mode (Bước 11)
`mode="shadow"` (vs `"human_approved"` mặc định) — chạy ĐẦY ĐỦ preflight (allowlist/unit-
exists/hash) nhưng KHÔNG gọi `run_guarded_recovery`. Trả `COMPLETED` (delivery layer coi
command đã xử lý xong) + `product_outcome=SHADOW_RECOMMENDATION` + evidence
`would_execute`/`predicted_verification_plan` — KHÔNG COMPLETED nghĩa mutation thật.

## Operator surface (Bước 12)
`python -m aoip.console.approve_systemd_restart --unit ... --tenant ... --approver ...
--mission-id ... --decision-id ... --incident-id ... --summary ...` → in JSON envelope hoàn
chỉnh (sẵn sàng POST `/webhook/agent/rt/commands/enqueue`). API response (`get_record`,
ĐÃ CÓ, không sửa) trả nguyên `outcome` dict = operator-facing structured result.

## TimingConfig wiring (theo brief)
`build_systemd_restart_executor(..., timing: TimingConfig | None = None)` — truyền
`execution_lease_ttl_s`/`lease_renewal_interval_s` vào `run_guarded_recovery`. KHÔNG wiring
`action_timeout_s`/`gateway_visibility_s` (operator `_sd_apply` hardcode 30s, Gateway
`_VISIBILITY_S` hardcode 60s — sửa cần đổi signature dùng chung, ngoài scope nhỏ nhất; ghi ở
remaining gaps).

## Implementation summary (Bước 4 deliverable)
- `src/aoip/capabilities/systemd_restart.py` (MỚI, ~330 dòng): toàn bộ capability contract,
  policy, hash-binding, decode, preflight, executor builder, outcome taxonomy, shadow mode.
- `src/aoip/console/approve_systemd_restart.py` (MỚI): CLI operator surface.
- Tests: `tests/test_capability_systemd_restart.py` (MỚI, 32 test — contract/policy/execution/
  verification/shadow), `tests/test_m1_systemd_recovery_e2e.py` (MỚI, 5 test — product E2E
  qua Gateway ASGI thật + DeliveryLoop thật).
- KHÔNG sửa `src/aoip/recovery.py`, `src/aoip/agent/operations.py`,
  `src/gateway/routes/agent_runtime.py` — capability layer hoàn toàn additive.

## Product E2E evidence (Bước 5 deliverable)
| Scenario | Kết quả |
|---|---|
| Happy path: enqueue → poll → guarded execute → verify → COMPLETED | PASS — `test_happy_path_incident_to_verified_recovery` |
| Approval rejected → zero mutation | PASS — `test_approval_rejected_never_executes` |
| Verification fail (restart rc=0 nhưng KHÔNG active) → ESCALATED | PASS — `test_verification_failure_escalates` |
| Shadow mode → recommendation, KHÔNG mutation | PASS — `test_shadow_mode_end_to_end_no_mutation` |
| Unit not allowlisted → BLOCKED_BY_POLICY | PASS — `test_unit_not_allowlisted_end_to_end` |
| Unit missing/payload tampered/unsupported version/expired approval/ownership-lost/duplicate-delivery | PASS ở tầng unit test (`test_capability_systemd_restart.py`, 32 test) |

## Operator-visible result (Bước 6 deliverable — ví dụ thật)
```json
{
  "capability": "systemd.restart_unit", "capability_version": "1",
  "attempted": "restart nginx.service", "target": {"unit": "nginx.service"},
  "mode": "human_approved", "approver": "alice",
  "product_outcome": "EXECUTED_AND_VERIFIED",
  "reason": "service + dependents verified",
  "evidence": {
    "capability_checks": [
      {"check": "unit_allowlisted", "ok": true, "detail": "unit=nginx.service allowlist=['nginx.service']"},
      {"check": "unit_exists", "ok": true, "detail": "systemctl show LoadState unit=nginx.service"},
      {"check": "capability_version_supported", "ok": true, "detail": "systemd.restart_unit@1"},
      {"check": "payload_hash_bound", "ok": true, "detail": "approved_payload_hash khớp payload nhận được"}
    ],
    "recovery_evidence": ["before=inactive", "service_health=ok", "dependents=n/a"]
  },
  "duration_s": 0.34, "next_step": "Không cần hành động thêm — service đã verified active."
}
```

## Test results (Bước 7 deliverable)

| Command | Result | Notes |
|---|---|---|
| `pytest tests/test_capability_systemd_restart.py -q` | 32 passed | contract/policy/execution/verification/shadow |
| `pytest tests/test_m1_systemd_recovery_e2e.py -q` | 5 passed | product E2E qua Gateway ASGI thật |
| `pytest tests/ -q -k "aoip or gateway_agent_runtime or capability or m1_systemd"` | 271 passed | toàn bộ regression liên quan |
| `pytest tests/ -q --ignore=tests/integration` (full suite) | 5856 passed, 1 failed | fail = `test_register_then_real_system_metrics_emitted_through_real_pipeline`, pre-existing xác nhận qua git stash các phiên trước, KHÔNG liên quan |
| Lint/type-check riêng | Not run | không có pyflakes/mypy sẵn trong `.venv`; `ast.parse` OK cho mọi file mới |

## Remaining product gaps (Bước 8 deliverable)

**Blocker cho SHADOW**: không có — SHADOW mode đã hoạt động đầy đủ (preflight thật, không
mutation), sẵn sàng dùng ngay.

**Blocker cho HUMAN_APPROVED lab**:
- Chưa có CLI/portal để REJECT approval một cách tường minh phía operator (hiện tại reject =
  không gọi CLI approve, hoặc tự set `approval.approved=false` thủ công trong payload —
  chưa có "reject" workflow riêng).
- `action_timeout_s`/`gateway_visibility_s` chưa parametrize qua TimingConfig (hardcode 30s/
  60s trong `recovery.py`/`agent_runtime.py`) — an toàn (renewal đã che phần lớn rủi ro) nhưng
  chưa "config wiring" đúng nghĩa cho 2 giá trị này.
- Chưa có Mission/Decision integration tự động (AnalystAdvisory → capability payload) —
  hiện tại CLI operator PHẢI tự điền mission_id/decision_id/incident_id thủ công; bridge từ
  advisory pipeline sang aoip Action/Decision là absent (đã note từ nghiên cứu Bước 1).

**Blocker cho AUTO_LOW_RISK**: TOÀN BỘ milestone này KHÔNG bật auto-execute production —
`env_auto_execute=False` mặc định, `requires_approval=True` cứng trong metadata. AUTO_LOW_RISK
cần: (a) risk-scoring tự động đủ tin cậy để bỏ qua approval cho 1 lớp rủi ro cụ thể, (b) chính
sách rate-limit/blast-radius bổ sung, (c) governance sign-off — KHÔNG nằm trong scope milestone
này (brief cấm rõ).

**Ngoài 3 nhóm trên (kế thừa từ phiên trước, không đổi)**: shared domain transition
enforcement tổng quát (ngoài `_advance`/`heartbeat_command`/`terminal_command`), payload
integrity/signing đầy đủ (PKI), typed capability certification chính thức (CASAN Automatic).

## CASAN classification
Milestone HOÀN THÀNH: **CASAN Standard** (typed action, governance qua approval+allowlist,
verification thật, audit hash-chain đầy đủ). CHUẨN BỊ (KHÔNG hoàn thành) **CASAN Automatic**:
SHADOW mode hoạt động; HUMAN_APPROVED hoạt động trong lab (FakeSystemd) — CHƯA chứng minh
trên host thật/production, CHƯA có AUTO_LOW_RISK governance. KHÔNG tuyên bố Automatic.

## Branch và commit
`feature/living-operations-runtime` @ `2391454` (HEAD trước phiên này). Phiên này CHƯA commit.

## Files chính đã thay đổi
`src/aoip/capabilities/systemd_restart.py` (mới), `src/aoip/console/approve_systemd_restart.py`
(mới), `tests/test_capability_systemd_restart.py` (mới), `tests/test_m1_systemd_recovery_e2e.py`
(mới). KHÔNG sửa file nào khác (capability layer hoàn toàn additive).

## Quyết định đã chốt
- KHÔNG dùng `aoip.algebra`/`aoip.primitives` (framework mô phỏng cũ, không có durable
  delivery/lease/fencing thật) — capability build trực tiếp trên `aoip.recovery`/
  `aoip.agent.operations` (Living Operations Runtime thật).
- Allowlist đặt ở CAPABILITY layer (`SystemdRestartPolicy`), KHÔNG thêm field vào
  `RecoveryGate` chung — tránh rủi ro đổi semantics generic gate dùng bởi capability khác
  trong tương lai.
- Approval-hash binding nhúng TRONG payload (`approved_payload_hash` field), KHÔNG mở rộng
  `Approval` dataclass (frozen, dùng ở nhiều nơi) — additive, zero risk cho code cũ.
- SHADOW mode báo cáo `COMPLETED` ở TẦNG DELIVERY (Gateway không biết/không cần biết khái
  niệm shadow) nhưng `product_outcome=SHADOW_RECOMMENDATION` ở tầng PRODUCT — tái dùng pattern
  đã có (`NO_ACTION_NEEDED` cũng COMPLETED-nhưng-zero-mutation).
- `_decode()`/`_classify_product_outcome()` capability-specific, KHÔNG sửa
  `decode_recovery_command`/`_map_recovery_outcome` chung trong `operations.py` (những hàm đó
  phục vụ generic recovery command, không riêng capability này — tránh capability framework
  lớn hơn cần thiết).
- `Approval.issue()` LUÔN trả `approved=True` (đã có từ trước) — `_decode()` PHẢI check
  `payload["approval"]["approved"]` TRƯỚC khi gọi `Approval.issue()`, không dựa vào field đó
  của kết quả (bug tự phát hiện qua test, đã fix trong phiên này).

## Verification đã chạy
- `pytest tests/test_capability_systemd_restart.py tests/test_m1_systemd_recovery_e2e.py -q` → 37 passed.
- `pytest tests/ -q -k "aoip or gateway_agent_runtime or capability or m1_systemd"` → 271 passed.
- `pytest tests/ -q --ignore=tests/integration` → 5856 passed, 1 failed (pre-existing,
  không liên quan, xác nhận các phiên trước bằng git stash).

## Deployment hiện tại
Không đổi. Capability mới CHƯA deploy — cần operator set
`AOIP_ALLOWED_SYSTEMD_UNITS=<unit1,unit2>` trong `/etc/aoip/agent.env` (cùng file với
`AOIP_AGENT_MODE=mutation_enabled` từ phiên trước) để bật thật trên VM. `daemon.py`
CHƯA wiring capability này làm executor mặc định (hiện tại `runtime_config.build_agent_runtime`
vẫn dùng `operations.build_recovery_executor` generic — capability-specific executor là
composition mới, cần wiring riêng nếu muốn dùng qua CLI/systemd, xem Next step).

## Blockers
None.

## Next step chính xác
1. Wiring `build_systemd_restart_executor` vào `daemon.main()`/`runtime_config.py` như một
   lựa chọn executor (thay vì generic `build_recovery_executor`) khi operator muốn CHỈ cho
   phép capability `systemd.restart_unit` (tighter scope hơn generic recovery).
2. Bridge AnalystAdvisory → Decision/capability payload (nếu muốn tự động hoá đề xuất, hiện
   tại operator phải tự chạy CLI `approve_systemd_restart`).
3. Reject-approval workflow tường minh (hiện tại thiếu, xem Remaining gaps).
4. Capability thứ hai (K8s deployment restart, DB action...) — KHÔNG làm trong phiên tới trừ
   khi M1 đã được operator xác nhận đủ tốt trong lab.

## Lệnh cần chạy lại
- `.venv/bin/python -m pytest tests/test_capability_systemd_restart.py tests/test_m1_systemd_recovery_e2e.py -q`
- `.venv/bin/python -m pytest tests/ -q -k "aoip or gateway_agent_runtime or capability or m1_systemd"`
- `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration`

## Không được làm lại
- KHÔNG thêm capability thứ hai trong milestone này.
- KHÔNG sửa `RecoveryGate`/`recovery.py`/`operations.py` để nhét thêm capability-specific
  logic — capability layer PHẢI ở `src/aoip/capabilities/systemd_restart.py`, additive.
- KHÔNG bật `env_auto_execute=True` mặc định hay giả định AUTO_LOW_RISK production.
- KHÔNG viết lại `Approval` dataclass để thêm field `approved` param cho `.issue()` — giữ
  nguyên "issue() luôn approved=True", check reject TRƯỚC khi gọi issue() ở capability layer.

## Tài liệu liên quan
- `src/aoip/capabilities/systemd_restart.py` (module docstring đầy đủ vertical slice).
- `src/aoip/console/approve_systemd_restart.py` (CLI operator surface).
- `tests/test_capability_systemd_restart.py`, `tests/test_m1_systemd_recovery_e2e.py`.
