# Changelog

Tất cả thay đổi đáng kể của AOIP runtime (`src/aoip/`) được ghi ở đây.
Định dạng theo Keep a Changelog; phiên bản theo SemVer.

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
