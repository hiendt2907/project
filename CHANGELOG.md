# Changelog

Tất cả thay đổi đáng kể của AOIP runtime (`src/aoip/`) được ghi ở đây.
Định dạng theo Keep a Changelog; phiên bản theo SemVer.

## [Unreleased] — Living Operations Runtime — Step 3: Durable Command Delivery

### Added — Durable delivery / acknowledgement / agent resume
- **P0 FIX — GET = PEEK, không POP.** Kênh command cũ (`gateway/routes/agent_commands.py`)
  dùng `RPOP` cho GET → command biến mất ngay khi fetch, trước bất kỳ terminal ack nào →
  agent crash sau fetch = mất lệnh. Kênh mutating recovery mới KHÔNG bao giờ pop khi fetch.
- **Durable delivery state machine** (`aoip/agent/delivery.py` + gateway twin
  `gateway/routes/agent_runtime.py`, prefix `/webhook/agent/rt`):
  `QUEUED → DELIVERED → ACCEPTED → RUNNING → RECONCILING → COMPLETED|FAILED|ESCALATED|EXPIRED`.
  Record mang identity + correlation bất biến (command/tenant/agent/mission/incident/decision/
  action/canonical_scope/payload_hash + created_at/expires_at/delivery_count/last_delivered_at/
  terminal_at). Redis: `omni:cmd:rec:{tenant}:{cid}` + ready-ZSET `omni:cmd:ready:{tenant}:{agent}`.
  Redelivery đến khi terminal ack durable; terminal ack idempotent (duplicate → zero re-mutation).
- **Agent durable local inbox** (`aoip/agent/inbox.py`): persist RECEIVED (fsync + atomic
  rename) TRƯỚC execute; local lifecycle `RECEIVED→ACCEPTED→RUNNING→OUTCOME_RECORDED→REPORTED→
  ACKED`. Resume sau restart/reboot: OUTCOME_RECORDED → re-report (KHÔNG re-mutate); RUNNING →
  reconcile (KHÔNG blind retry). Chỉ archive khi có terminal ack.
- **Delivery loop + daemon** (`aoip/agent/delivery_loop.py`, `aoip/agent/daemon.py`): register→
  heartbeat→resume→pull→persist→execute→report→sleep→repeat. systemd unit
  `deploy/systemd/aoip-agent.service` (StateDirectory giữ inbox qua reboot, SIGTERM sạch).
  `HTTPOmniClient` thêm poll_runtime/accept/progress/report_terminal.
- **Proof**: 25 unit/integration tests (`test_aoip_delivery`, `test_aoip_delivery_loop`,
  `test_aoip_agent_daemon`, `test_gateway_agent_runtime` qua ASGI thật) phủ 8 case DoD.
  Harness hạ tầng thật: `scripts/prove_durable_delivery.py` (Gateway/Redis K8s + VM systemd).

## [0.1.0] — 2026-06-30 — "Controlled Recovery"

First controlled-recovery release. AOIP runtime (Autonomous SRE) đi từ Discovery →
Understand → Operate, chạy THẬT trên VM Linux (OrbStack lab qua transport, không phải
dependency kiến trúc). Vượt ranh giới: không chỉ HIỂU vì sao hỏng, mà PHỤC HỒI có
kiểm soát, bằng chứng, trách nhiệm.

### Added — Operate
- **Controlled Recovery** (`recovery.py`, `audit.py`): vòng fail-closed verified →
  approve (HITL) → execute → verify (service + dependents) → COMPLETED | escalate.
  Recovery theo `(failure_mode, substrate)`, KHÔNG theo service name — operator
  `(process_down, systemd)` phục hồi redis-server/mariadb/nginx chung 1 path. Gate:
  incident verified · diagnosis positive+fresh · explicit approval · action APPROVED ·
  capability · risk · scope · current-state-broken. KHÔNG retry. `OMNI_AUTO_EXECUTE_
  ENABLED=false` fail-closed. Audit SHA-256 hash-chain tamper-evident, host-side
  (INV_DATA_RESIDENCY), event họ CRAT.
- **Diagnosis Engine** (`diagnosis.py`, `capability_catalog.py`, `capability_diagnosis.py`,
  `failure_modes.py`): multi-hypothesis falsification, domain-agnostic core. Probe
  ba trạng thái PRESENT/ABSENT/UNAVAILABLE (UNAVAILABLE không phải counter-evidence).
  `capability_tags` (nhiều tag/service, confidence + provenance; port/name = giả
  thuyết, không Fact). Confidence = score nội bộ (chưa calibrate thành xác suất).
- **Decision Engine** (`decision.py`): Incident → Candidate Actions → Decision; chọn
  rủi ro nhỏ nhất giải quyết (INV_SMALL_BLAST_RADIUS); recovery_confidence =
  action_conf × diagnosis_conf.
- **Incident Understanding** (`incident.py`, `system_model.py::blast_radius`): verify
  probe thật → blast radius bằng graph traversal (không LLM).

### Added — Discover & Understand
- Real Linux discovery (`remote_linux_backend.py`) + transport abstraction
  (`transport.py`: Local/SSH/Orb-lab). Agent bootstrap (`agent/`, `scripts/install_agent.sh`).
- Mission Runtime (`mission.py`): capability = composition + Definition-of-Done;
  KPI = Mission Completion.
- Knowledge Graph (`system_graph.py`, `system_model.py`): typed nodes/edges, projections.
- Evidence Completion Engine (`evidence.py`): INV_INFER_BEFORE_ASK — infer trước khi hỏi.
- Learning loop (`knowledge/store.py`): human answer → persistent Fact, sống sót
  restart/reinstall.
- OmniClient abstraction (`agent/omni_client.py`): AOIP KHÔNG sở hữu control plane;
  `HTTPOmniClient` gọi gateway thật.

### Notes
- `live_*.py` là harness chứng minh trên máy thật (executable proofs), giữ lại.
- Ontology đóng băng: KHÔNG noun mới toàn milestone (Operator/Gate/Approval/Outcome
  /CapabilityTag/ScoredAction đều là Derived).
- 102 AOIP tests pass.
