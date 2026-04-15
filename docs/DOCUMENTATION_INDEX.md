# Omni — chỉ mục tài liệu (một bản đồ)

Mục tiêu: **giảm rời rạc** — mọi file dưới đây được xếp **tầng**; đọc vận hành thật luôn bắt đầu từ **Tầng 0**.

---

## Tầng 0 — bắt buộc đọc trước

| File | Vai trò |
|------|--------|
| [vendor/OMNI_PROJECT_CANONICAL.md](vendor/OMNI_PROJECT_CANONICAL.md) | **Một nguồn** kiến trúc + Kafka + RAG + verify (bám code). |
| [vendor/OMNI_LABEL_SCHEMA.md](vendor/OMNI_LABEL_SCHEMA.md) | Golden Link: Resource / Signal / Telemetry / Incident / Resolution labels. |
| [vendor/knownbase.md](vendor/knownbase.md) | Symptom → fix (sau incident). |
| [reports/project-memory.md](reports/project-memory.md) | Invariants, failure patterns, guardrails. |

---

## Tầng 1 — vận hành & playbook

| File | Vai trò |
|------|--------|
| [omni_playbook_index.md](omni_playbook_index.md) | Pointer RAG / retrieval surfaces + corpus contract. |
| [vendor/master_plan_v3_review_report.md](vendor/master_plan_v3_review_report.md) | MPV3 review, lịch sử, §15 nợ. |
| [vendor/adr-rbac-executor.md](vendor/adr-rbac-executor.md) | ADR RBAC executor (nháp). |
| [vendor/golden_path_split.md](vendor/golden_path_split.md) | Bookmark → redirect tới canonical. |
| [proactive_slo.md](proactive_slo.md) | PromQL proactive / metrics theo deployment. |
| [proactive_state_machine.md](proactive_state_machine.md) | Phase proactive. |
| [mcp_integration.md](mcp_integration.md) | ADR MCP pilot. |
| [runbooks/](runbooks/) | Checklist release, trace proof, E2E matrix, test evidence. |
| [../tests/integration/README.md](../tests/integration/README.md) | Integration E2E: stateful ReAct harness (`test_e2e_autonomous_loop`), glassbox audit (`OMNI_E2E_AUDIT_JSON`), two-phase planner (mutate not in-process). |
| [reports/lab_nginx_missing_configmap_e2e.md](reports/lab_nginx_missing_configmap_e2e.md) | Lab một luồng: nginx-test thiếu ConfigMap → gateway → prober evidence FailedMount. |
| [reports/trace-audit-gw-prom-f58ffe43e85e.md](reports/trace-audit-gw-prom-f58ffe43e85e.md) | Trace audit (Loki + pipeline + fixes) — lab missing ConfigMap. |
| [runbooks/claude_code_prompt_trace_memory.md](runbooks/claude_code_prompt_trace_memory.md) | Prompt copy-paste cho Claude Code: verify trace + sync `docs/` + `.claude/MEMORY.md`. |
| [../src/workers/memory/initial_symptom.py](../src/workers/memory/initial_symptom.py) | **InitialSymptom** (Prometheus/Alertmanager) → `OmniTraceMemory` + `<INITIAL_SYMPTOM>` trong planner; `run_agentic_mutate_plan` reload Redis mỗi vòng ReAct. Env: `OMNI_PLANNER_LLM_SOLE_EVALUATOR` (tắt regex hints credential/broken-spec), `OMNI_TRACE_MEMORY_TOOL_OUTPUT_MAX_CHARS`. |

---

## Tầng 2 — kiến trúc & spec (chi tiết, có thể lệch thời điểm)

Thư mục [architecture/](architecture/) — contract, SLO gates, state machine, bắt tay adapter:

| File |
|------|
| [architecture/adapter_contracts.md](architecture/adapter_contracts.md) |
| [architecture/autonomy_slo_gates.md](architecture/autonomy_slo_gates.md) |
| [architecture/autonomy_state_machine.md](architecture/autonomy_state_machine.md) |
| [architecture/autonomy_test_strategy.md](architecture/autonomy_test_strategy.md) |
| [architecture/north_star_spec.md](architecture/north_star_spec.md) |
| [architecture/security_policy_by_adapter.md](architecture/security_policy_by_adapter.md) |
| [architecture/transition_contract.md](architecture/transition_contract.md) |

Tài liệu tóm tắt / slide / whitepaper (có thể mô tả “một worker” tập trung — đối chiếu canonical):

| File |
|------|
| [omni_v3_architecture_whitepaper.md](omni_v3_architecture_whitepaper.md) |
| [omni_v3_executive_report.md](omni_v3_executive_report.md) |

---

## Tầng 3 — báo cáo phase & chaos (artifact theo thời điểm)

| Vị trí | Ghi chú |
|--------|---------|
| [reports/](reports/) (trong `docs/`) | Phase 1–7, chaos-rag-selflearn, templates, dashboard SoT — **danh sách đầy đủ:** [reports/README.md](reports/README.md). |
| [reports/tech-debt-remediation-plan.md](reports/tech-debt-remediation-plan.md) | Kế hoạch xử lý technical debt (trừ fleet). |
| [reports/rag-gate-observability.md](reports/rag-gate-observability.md) | Log grep + env chỉnh RAG gate. |
| [reports/ci-verification-report.md](reports/ci-verification-report.md) | Báo cáo xác minh build/deploy/gate/pytest (snapshot). |
| [reports/incident-evidence-three-lanes.md](reports/incident-evidence-three-lanes.md) | Proof-of-Fault: lane `resource` / `state` / `app_log` + matrix. |
| [reports/diagnostic-policy-spec.md](reports/diagnostic-policy-spec.md) | **Diagnostic Policy:** INV_* invariants, discovery tool map, ReAct O/H/V/A, blind `proof_lane`, `reasoning_chain` on `SUGGEST_REMEDIATION`. Code: `pkg/reasoning/diagnostic_policy.py`, gate in `evidence_consumer`. |
| [reports/sigma-log-bypass-spec.md](reports/sigma-log-bypass-spec.md) | Loki sustained-5xx bypass (điều kiện, env, fail-closed). |
| [runbooks/sigma-log-bypass-ops.md](runbooks/sigma-log-bypass-ops.md) | Bật/tắt bypass lab, verify grep. |
| [reports/e2e-dual-trace-log-analysis-20260407.md](reports/e2e-dual-trace-log-analysis-20260407.md) | Hai trace E2E (`gw-prom-d7796b45517d` lab contrast + `gw-prom-21e83e390b09` waiting fault): phân tích từng dòng prober/analyst/executor. |
| [reports/e2e-nginx-waiting-fault-log-analysis.md](reports/e2e-nginx-waiting-fault-log-analysis.md) | E2E fault `nginx_waiting_fault`: phân tích log theo dòng + `trace_id`. |
| [reports/alert-flow-realistic-test-plan.md](reports/alert-flow-realistic-test-plan.md) | Kế hoạch test luồng alert thực (staging / replay / soak). |
| [reports/alert-flow-realistic/PHASE0_CHECKLIST.md](reports/alert-flow-realistic/PHASE0_CHECKLIST.md) | Checklist Phase 0 trước khi inject fault. |
| [phase_report_template.md](phase_report_template.md), [phase_review_template.md](phase_review_template.md) | Template báo cáo / review phase. |

---

## Sprint 7 — Stateful Closed-Loop (April 2026)

### Core Files

| File | Vai trò |
|------|---------|
| `scripts/mvp_api.py` | Stateful loop (`for iteration in range(1, MAX+1)`); `phase5_verify()` kind-aware SDK health check; GAP-05 OLLAMA auto-detection; 5s backoff. |
| `src/pkg/autonomy/llm_contract.py` | `RemediationContext` + `ObservationRecord` / `ActionRecord` / `OutcomeRecord`; `to_prompt_block()` serialises history into LLM system prompt. |
| `src/workers/k8s_cluster_tools.py` | `get_resource_owner(pod, ns)` — recursive OwnerReference traversal (Truth Layer): Pod→RS→Deployment/StatefulSet. |

### Tests & Coverage

```bash
# Golden test suite with coverage
pytest --cov=src --cov=scripts tests/ --cov-report=term-missing

# Fast run
pytest tests/test_omni_stateful_loop.py -q
```

| File | Tests | Coverage (target modules) |
|------|-------|--------------------------|
| `tests/test_omni_stateful_loop.py` | 70 | `mvp_api.py` 58%, `llm_contract.py` 98%, `k8s_cluster_tools.py` 40% |
| `tests/conftest.py` | — | Adds `src/` + `scripts/` to sys.path |
| `.coveragerc` | — | Coverage config (`source = src, scripts`) |

### Scenarios Covered

| Scenario | Lane | Key assertion |
|----------|------|--------------|
| 2-iteration ConfigMap recovery | STATE | `converged=True` iter 2; history in LLM prompt verified |
| Single-iteration OOMKilled convergence | STATE | `iterations=1`, `converged=True`, `executed=True` |
| Exec skipped outside lab mode | STATE | `resolution_state='exec_skipped'`, `executed=False` |
| Security gate (`INV_NAMESPACE_ISOLATION`) | STATE | Loop terminates iter 1; execute never called |
| Max retries (3 failed verifies) | STATE | `iterations==3`, `converged==False`, `resolution_state='exhausted'` |
| 3-sigma gate blocked | RESOURCE | `resolution_state='sigma_gate'`; LLM never called |
| Gate skipped (Redis unavailable) | RESOURCE | LLM called; plan returned |
| Anomaly detected → execute → converge | RESOURCE | `is_anomaly=True`; `converged=True` |
| Fail-closed Loki (no `LOKI_BASE_URL`) | APP_LOG | LLM never called; `loki_unavailable=True` |
| Loki surge ok → LLM → converge | APP_LOG | `log_surge_ok=True`; `converged=True` |
| Loki surge ok=False → LLM → noop | APP_LOG | `log_surge_ok=False`; `resolution_state='incomplete'` |
| Phase 1 parse — OOMKilled → STATE | — | `laned.lane is Lane.STATE` |
| Phase 1 parse — HighCPUUsage → RESOURCE | — | `laned.lane is Lane.RESOURCE` |
| Phase 1 parse — HttpErrorRate5xx → APP_LOG | — | `laned.lane is Lane.APP_LOG` |
| Phase 1 parse — app_log downgrade (non-api_web) | — | `lane_source='api_web_guard'` |
| `HighLevelRemediationPlan` validators (all 5 actions) | — | Raises `ValidationError` on bad input |
| `map_high_level_plan_to_mutate` all branches + defaults | — | Correct `tool_name` + `args` per action |
| `parse_high_level_plan_json` fence-strip + prose-embed + invalid | — | None on unparseable; plan on valid |

### UAT Report

[../reports/chaos/uat-stateful-loop-2026-04-10.md](../reports/chaos/uat-stateful-loop-2026-04-10.md) — Full state-transition log with OwnerRef-based verification (v3, final).

**Key invariants:**
- `RemediationContext.to_prompt_block()` injects history into LLM system prompt on iterations > 1.
- `get_resource_owner` follows `metadata.ownerReferences` only — no pod-name heuristics.
- `phase5_verify` is kind-aware (Deployment / StatefulSet); no fault-type branching.
- `_shadow_writeback` called only on `converged=True`.
- `MAX_LOOP_ITERATIONS=3` (`OMNI_MAX_LOOP_ITERATIONS`); backoff 5s (`OMNI_VERIFY_BACKOFF_SECONDS`).

---

## Tầng 4 — `reports/` ở root repo (không trùng `docs/reports/`)

Báo cáo / flow cũ theo phase hoặc monolith — **đối chiếu canonical** khi đọc lệnh `kubectl` / topology:

| File |
|------|
| [../reports/e2e-telegram-flow.md](../reports/e2e-telegram-flow.md) |
| [../reports/phase1-infrastructure.md](../reports/phase1-infrastructure.md) |
| [../reports/phase2-core.md](../reports/phase2-core.md) |
| [../reports/phase3-tools-visuals.md](../reports/phase3-tools-visuals.md) |
| [../reports/phase4-omni-worker.md](../reports/phase4-omni-worker.md) |
| [../reports/chaos-rag-selflearn/registry-template.md](../reports/chaos-rag-selflearn/registry-template.md) |

---

## Tầng 5 — file `*.md` ở root repo (planning snapshot / phân tích)

**Không** coi là SSoT vận hành — có banner trỏ canonical trong từng file.

| File | Ghi chú ngắn |
|------|----------------|
| [../README.md](../README.md) | Cổng repo. |
| [../architecture_analysis.md](../architecture_analysis.md) | Phân tích kiến trúc (có thể legacy Redis stream). |
| [../alert_flow_analysis.md](../alert_flow_analysis.md) | Phân tích luồng alert. |
| [../auto_fix_plan.md](../auto_fix_plan.md) | Plan self-healing. |
| [../cleanup_plan.md](../cleanup_plan.md) | Plan cleanup. |
| [../gateway_fix_plan.md](../gateway_fix_plan.md) | Plan gateway Redis. |
| [../rebuild_image_plan.md](../rebuild_image_plan.md) | Plan rebuild image. |
| [../watchdog_plan.md](../watchdog_plan.md) | Plan watchdog. |
| [../incident_report.md](../incident_report.md) | Báo cáo incident (point-in-time). |

---

## Tầng 6 — vendor mirror & công cụ

| Vị trí | Ghi chú |
|--------|---------|
| [vendor/README.md](vendor/README.md) | Mirror tài liệu ngoài (`sources.json`, sync script). |
| [vendor/technical_debt_blackbook.md](vendor/technical_debt_blackbook.md) | Stub → trỏ MPV3 §15. |
| [vendor/refactor_unified_roadmap.md](vendor/refactor_unified_roadmap.md) | Roadmap refactor + link plan Cursor. |
| [vendor/autonomous_state_machine.md](vendor/autonomous_state_machine.md) | State machine autonomous. |
| `vendor/*/INDEX.md` | Index từng dependency (Python, K8s, …). |

---

## Tầng 7 — khu vực khác

| Vị trí | Ghi chú |
|--------|---------|
| [../tests/FEATURE_MATRIX.md](../tests/FEATURE_MATRIX.md) | Ma trận feature test. |
| [../data/knowledge_local/README.md](../data/knowledge_local/README.md) | Knowledge local sample. |
| [../data/knowledge_local/sample_ops.md](../data/knowledge_local/sample_ops.md) | Sample ops knowledge. |
| [../k8s/monitor/MIGRATION_VM_TO_PROMETHEUS.md](../k8s/monitor/MIGRATION_VM_TO_PROMETHEUS.md) | Ghi chú migrate monitor. |
| [../k8s/monitor/MIMIR.md](../k8s/monitor/MIMIR.md), [../k8s/monitor/GRAFANA_ALERTING.md](../k8s/monitor/GRAFANA_ALERTING.md) | Monitor stack notes. |
| [../k8s/opensandbox/README.md](../k8s/opensandbox/README.md) | Open sandbox cluster notes. |
| [../.cursor/skills/ai-agentic/SKILL.md](../.cursor/skills/ai-agentic/SKILL.md) | Skill agentic Cursor. |
| [vendor/redis_sentinel_lab.md](vendor/redis_sentinel_lab.md) | Lab Redis Sentinel (vendor-style). |
| [vendor/master_plan_v3_phase05_report.md](vendor/master_plan_v3_phase05_report.md) | MPV3 phase 05 report snapshot. |

---

## Quy tắc thêm doc mới

1. **Kiến trúc / vận hành runtime** → cập nhật [vendor/OMNI_PROJECT_CANONICAL.md](vendor/OMNI_PROJECT_CANONICAL.md) hoặc PR riêng; **không** thêm file “SSoT” thứ hai.  
2. **Symptom sau incident** → [vendor/knownbase.md](vendor/knownbase.md).  
3. **Invariant / guardrail** → [reports/project-memory.md](reports/project-memory.md).  
4. **Báo cáo theo sprint / phase** → `docs/reports/` hoặc `reports/` + **một dòng** trong bảng tầng tương ứng tại chỉ mục này (nếu doc đủ quan trọng).  
5. **Vendor mirror** → chỉ dưới `docs/vendor/` + `sources.json`.

---

*File này là **điểm vào duy nhất** để điều hướng doc; không nhân đôi nội dung dài từ canonical.*
