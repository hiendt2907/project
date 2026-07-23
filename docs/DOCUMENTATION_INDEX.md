# Omni — chỉ mục tài liệu

> Viết lại 2026-07-22 sau khi dọn sprawl (248 → 73 file). Mọi file dưới đây đang **current** —
> nếu nghi ngờ một file đã lệch thực tế, đối chiếu với `../CLAUDE.md` (nguồn sự thật) hoặc code.

## Tầng 0 — bắt buộc đọc trước

| File | Vai trò |
|------|--------|
| [`../CLAUDE.md`](../CLAUDE.md) | Nguồn sự thật duy nhất: kiến trúc, invariant, deployment state, lệnh vận hành. |
| [`CODEBASE.md`](CODEBASE.md) | Bản đồ file-level: module index, Kafka topic map, Redis key map, data flow. |
| [`../MEMORY.md`](../MEMORY.md) | Pointer ngắn — đọc cùng CLAUDE.md đầu mỗi task. |
| [`handoffs/CURRENT_SESSION.md`](handoffs/CURRENT_SESSION.md) | Handoff phiên gần nhất — deliverable, next step. |

## Tầng 1 — audit & capability

| File | Vai trò |
|------|--------|
| [`architecture/ASSESSMENT_autonomous_sre_v2.md`](architecture/ASSESSMENT_autonomous_sre_v2.md) | Ma trận năng lực 18-domain (runtime-verified) + assessment kiến trúc gốc so với vision Autonomous SRE. |
| [`architecture/AUDIT_autonomous_sre_team_2026_07_22.md`](architecture/AUDIT_autonomous_sre_team_2026_07_22.md) | Audit SIEM + remote agent, 76-hàng, runtime-verified. |
| [`architecture/TECH_DEBT_BACKLOG.md`](architecture/TECH_DEBT_BACKLOG.md) | Nợ kỹ thuật đang tracked. |
| [`product/PRODUCT_PROOF.md`](product/PRODUCT_PROOF.md) | Capability matrix operator-visible (ADR-003 legend), golden journey, iteration log. |
| [`product/PRODUCT_CONTRACT.md`](product/PRODUCT_CONTRACT.md) | Product contract (§10 tham chiếu ADR-003). |
| [`product/PRODUCTION_MISSON.md`](product/PRODUCTION_MISSON.md) | Mission/nguyên tắc bất biến cho productization. |
| [`post-mortems/drift-correction-2026-07-02.md`](post-mortems/drift-correction-2026-07-02.md) | Post-mortem kill-switch bị bỏ quên `=true`; duy nhất còn giữ (còn lại đã xoá — scratch/superseded). |

## Tầng 2 — kiến trúc chi tiết (`architecture/`)

**ADR (Architecture Decision Records — giữ vĩnh viễn, lịch sử quyết định):**
[ADR-001](architecture/ADR-001-canonical-agent-runtime.md) (agent runtime AOIP vs remote_agent) ·
[ADR-002](architecture/ADR-002-command-protocol.md) (command protocol) ·
[ADR-003](architecture/ADR-003-backend-frontend-parity.md) (backend/frontend parity) ·
[ADR-004](architecture/ADR-004-runtime-convergence.md) (workers vẫn execution engine, AOIP là control-plane) ·
[ADR-005](architecture/ADR-005-recovery-executor-consolidation.md) · [ADR-006](architecture/ADR-006-evidence-command-contract-convergence.md).

[`customer-system-understanding.md`](architecture/customer-system-understanding.md) — canonical cho System Twin/topology khách hàng (loại trừ Omni/Remote Agent khỏi view chính).

**AOIP ontology / "Constitution" (frozen 2026-06-29, KHÔNG mở rộng — xem `../../MASTER_PLAN.md`):**
`FRAMEWORK_LAWS.md`, `META_MODEL.md`, `SEMANTIC_RULES.md`, `CAPABILITY_MODEL.md`,
`ORGANIZATION_MODEL_sre.md`, `OPERATING_MODEL_sre.md`, `COGNITIVE_MODEL_sre.md`,
`KNOWLEDGE_MODEL_sre.md`, `LEARNING_MODEL.md`, `DOMAIN_MODEL_autonomous_sre.md`,
`EXECUTION_MODEL.md`, `north_star_spec.md`, `transition_contract.md`, `autonomy_state_machine.md`,
`autonomy_slo_gates.md`, `autonomy_test_strategy.md`, `adapter_contracts.md`,
`security_policy_by_adapter.md`, `ASSESSMENT.md`. Đây là vocabulary/ontology mà `src/aoip/`
implement theo (xem docstring `src/aoip/__init__.py`) — **status DESIGN ONLY, không phải code**;
implementation là source of truth khi có mâu thuẫn. Không viết thêm file kiểu này — `MASTER_PLAN.md`
đã tuyên bố "ontology phase COMPLETE", ưu tiên code không phải doc mới.

## Tầng 3 — vận hành

| Khu vực | File |
|---|---|
| Runbooks (current) | [`auto-execute-policy-matrix.md`](runbooks/auto-execute-policy-matrix.md) · [`demo-four-streams-telegram.md`](runbooks/demo-four-streams-telegram.md) · [`llm-orbstack-urls.md`](runbooks/llm-orbstack-urls.md) · [`shadow_os_command_mode.md`](runbooks/shadow_os_command_mode.md) · [`siem-capability-proof.md`](runbooks/siem-capability-proof.md) · [`sigma-log-bypass-ops.md`](runbooks/sigma-log-bypass-ops.md) |
| Lanes | [`lanes/lane1_resource.md`](lanes/lane1_resource.md) · [`lanes/lane2_sys_hard_fail.md`](lanes/lane2_sys_hard_fail.md) |
| Design | [`design/trace-orchestrator.md`](design/trace-orchestrator.md) |
| Operations | [`operations/AUTONOMOUS_LOOP_LEDGER.md`](operations/AUTONOMOUS_LOOP_LEDGER.md) — ledger vòng lặp `omni-lane-operator-loop` (iter1-27) |
| Session automation | [`engineering/claude-session-automation.md`](engineering/claude-session-automation.md) |
| Handoffs | [`handoffs/CURRENT_SESSION.md`](handoffs/CURRENT_SESSION.md) · [`handoffs/PHASE_0_6_PROGRESS.md`](handoffs/PHASE_0_6_PROGRESS.md) · [`handoffs/TEMPLATE.md`](handoffs/TEMPLATE.md) (mẫu cho handoff mới) |
| Plans (đang active) | [`plans/`](plans/) — xem README ngắn trong từng file; `aoip-portals-identity-foundation.md`, `aoip-provider-portal-slices.md`, `it4-collector-parity-checklist.md`, `living-operations-hardening.md`, `remote-agent-knowledge-architecture.md`, `sprint-agent-sre-employee-production.md` |
| RAG / playbook pointer | [`omni_playbook_index.md`](omni_playbook_index.md) |
| Proactive loop | [`proactive_slo.md`](proactive_slo.md) · [`proactive_state_machine.md`](proactive_state_machine.md) |
| MCP pilot | [`mcp_integration.md`](mcp_integration.md) |
| Label schema | [`vendor/OMNI_LABEL_SCHEMA.md`](vendor/OMNI_LABEL_SCHEMA.md) — Golden Link labels, machine-readable: `config/omni_label_schema.yaml` |
| Contributing | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

## Tầng 4 — reports (point-in-time, xem [`reports/README.md`](reports/README.md))

Chỉ còn 6 report còn current (đã xoá phase-1..7, chaos-rag snapshot, audit cũ — superseded bởi
Tầng 1 ở trên): `frontend-backend-logic-verification-2026-07-14.md` (release gate mới nhất),
`project-memory.md` (invariants/failure-patterns, entries ≥2026-07-14 current), `diagnostic-policy-spec.md`,
`sigma-log-bypass-spec.md`, `incident-evidence-three-lanes.md`, `dashboard-source-of-truth.md`.

## Quy tắc thêm doc mới

1. **Kiến trúc / vận hành runtime** → cập nhật `CLAUDE.md` hoặc `CODEBASE.md` trực tiếp; **không**
   tạo file "canonical" thứ hai.
2. **Quyết định kiến trúc** → ADR mới trong `architecture/` (`ADR-NNN-slug.md`).
3. **Audit / capability tổng thể** → cập nhật trong `ASSESSMENT_autonomous_sre_v2.md` (ma trận ở
   đầu file), không tạo báo cáo audit rời rạc mới.
4. **Symptom sau incident thật, đáng nhớ lâu dài** → thêm vào `reports/project-memory.md`.
5. **Báo cáo point-in-time** (test run, verification snapshot) → chỉ giữ nếu còn giá trị tham
   chiếu sau khi việc đã xong; ưu tiên viết vào handoff (`handoffs/CURRENT_SESSION.md`) trước, chỉ
   "thăng cấp" lên file report riêng nếu sẽ được đọc lại nhiều lần.
6. **Vendor mirror kỹ thuật (Redis/K8s/Prometheus...)** — đã bị xoá 2026-07-22 (31MB HTML cache
   không dùng); nếu cần lại: `python scripts/sync_vendor_docs.py`.

*File này là điểm vào duy nhất để điều hướng `docs/`; không nhân đôi nội dung dài từ CLAUDE.md.*
