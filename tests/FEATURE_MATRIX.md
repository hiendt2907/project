# Feature ↔ test matrix

Ánh xạ **vùng code `src/`** → **file test** và lệnh kiểm tra nhanh. Cập nhật khi thêm module/test mới.

| Domain (`src/`) | Primary modules | Test files | Verify (targeted) |
|-----------------|-----------------|------------|-------------------|
| **gateway** | `gateway/api.py` | `test_gateway_chaos_silence.py` | `pytest tests/test_gateway_chaos_silence.py -v` |
| **workers** (handlers, session, tools) | `handlers`, `session_state`, `ollama_semaphore` | `test_handlers.py`, `test_handlers_inbound_preview.py`, `test_conversational_fallback.py`, `test_tool_prompt_parity.py` | `pytest tests/test_handlers*.py tests/test_conversational_fallback.py tests/test_tool_prompt_parity.py -v` |
| **workers** (inbound / infra / charts) | `clarification`, `infra_preflight`, handlers | `test_clarification_flow.py`, `test_clarification_context.py`, `test_end_to_end_clarification.py`, `test_infra_preflight_clarification.py`, `test_host_to_chart_continuity.py` | `pytest tests/test_*clarification*.py tests/test_infra_preflight_clarification.py tests/test_host_to_chart_continuity.py -v` |
| **workers** (proactive / metrics export) | `proactive_observer`, `metrics_exporter`, `settings` | `test_proactive_*.py`, `test_metrics_command_center.py`, `test_acceptance_daemon.py`, `test_worker_settings.py` | `pytest tests/test_proactive_*.py tests/test_metrics_command_center.py tests/test_acceptance_daemon.py tests/test_worker_settings.py -v` |
| **workers** (agentic / autonomous) | `agentic_slow_path`, `autonomous_decider`, `autonomous_route`, `baseline_snapshot` | `test_agentic_slow_path.py`, `test_autonomous_decider.py`, `test_autonomous_route.py`, `test_baseline_snapshot.py` | `pytest tests/test_agentic_slow_path.py tests/test_autonomous_*.py tests/test_baseline_snapshot.py -v` |
| **workers** (K8s / registry / approval) | `k8s_tools`, `k8s_cluster_tools`, `tool_registry`, `tool_approval`, … | `test_k8s_tools.py`, `test_v3_tools.py` | `pytest tests/test_k8s_tools.py tests/test_v3_tools.py -v` |
| **workers** (SDK / promql / analytics) | `sdk_service_tools`, `promql_presets`, `analytics_ts`, `request_trace` | `test_sdk_service_tools.py`, `test_auto_promql_generation.py`, `test_analytics_ts.py`, `test_request_trace.py`, `test_expert_tools.py`, `test_observability_self_heal.py`, `test_proactive_slo_and_grounding.py` | `pytest tests/test_sdk_service_tools.py tests/test_auto_promql_generation.py tests/test_analytics_ts.py tests/test_request_trace.py tests/test_expert_tools.py tests/test_observability_self_heal.py tests/test_proactive_slo_and_grounding.py -v` |
| **workers** (routing / slow path) | `routing_policy`, `slow_path_trace`, `model_routing` | `test_routing_experience.py`, `test_routing_policy_god.py`, `test_slow_path_trace.py`, `test_model_routing.py` | `pytest tests/test_routing_*.py tests/test_slow_path_trace.py tests/test_model_routing.py -v` |
| **workers** (tooling misc) | `tool_backend`, `llm_context_budget`, `proactive_guardrails`, `proactive_tool_policy` | `test_tool_backend.py`, `test_llm_context_budget.py`, `test_proactive_guardrails.py`, `test_proactive_tool_policy.py` | `pytest tests/test_tool_backend.py tests/test_llm_context_budget.py tests/test_proactive_guardrails.py tests/test_proactive_tool_policy.py -v` |
| **llm** | `llm/ollama_client.py` | `test_ollama_client.py` | `pytest tests/test_ollama_client.py -v` |
| **rag** | `error_ledger`, `pgvector_store` | `test_error_ledger.py`, `test_pgvector_stable_vec.py` | `pytest tests/test_error_ledger.py tests/test_pgvector_stable_vec.py -v` |
| **training** | `sop_ingest`, `sop_expand`, `cli_hil_*` | `test_sop_ingest.py`, `test_sop_expand.py`, `test_sop_expand_god.py`, `test_cli_hil.py` | `pytest tests/test_sop_*.py tests/test_cli_hil.py -v` |
| **observability** | `observability/normalize.py` | `test_normalize.py` | `pytest tests/test_normalize.py -v` |
| **metrics** | `metrics/prometheus_dataframe.py` | `test_metrics_prometheus_df.py` | `pytest tests/test_metrics_prometheus_df.py -v` |
| **visualization** | `visualization/chart_bytes.py` | `test_chart_bytes.py`, `test_chart_ci_bytes.py` | `pytest tests/test_chart_*.py -v` |
| **anomaly** | `anomaly/forecast.py`, `three_sigma.py`, `prophet_forecast.py` | `test_forecast.py`, `test_three_sigma.py`, `test_prophet_forecast.py` | `pytest tests/test_forecast.py tests/test_three_sigma.py tests/test_prophet_forecast.py -v` |
| **execution** | `experience`, `memory_normalize`, `policy`, `manager` | `test_routing_experience.py`, `test_experience_lesson.py`, `test_memory_normalize.py`, `test_policy_denylist.py`, `test_sandbox_manager.py` | `pytest tests/test_experience_lesson.py tests/test_memory_normalize.py tests/test_policy_denylist.py tests/test_sandbox_manager.py -v` |
| **init** | `deep_scout`, `deep_scout_autonomous` | `test_deep_scout.py`, `test_deep_scout_autonomous_unit.py` | `pytest tests/test_deep_scout*.py -v` |
| **ingest** | `ingest/telegram.py` | `test_telegram.py` | `pytest tests/test_telegram.py -v` |
| **scripts** (repo tooling, không nằm trong `src/`) | `scripts/chaos_drill_v1.py`, `simulate_dual_flow_15m.py` | `test_chaos_drill_v1.py`, `test_simulate_dual_flow.py` | `pytest tests/test_chaos_drill_v1.py tests/test_simulate_dual_flow.py -v` |
| **integration / chaos wrappers** | subprocess → `scripts/agentic_chaos_validation.py` | `test_agentic_chaos_validation.py` | `pytest tests/test_agentic_chaos_validation.py -v` |

## Full suite & evidence

- Toàn bộ: `make test-evidence` hoặc `bash scripts/run_test_evidence.sh` (JUnit + log + meta trong `evidence/latest/`).
- **Collect count:** `pytest tests --collect-only -q` → **312 tests** (theo snapshot repo).

## Gaps / manual

| Area | Note |
|------|------|
| `sre/watchdog.py` | Không thấy test trực tiếp trong `tests/` — xem SRE smoke / K8s. |
| `opensandbox_shim/` | Hành vi shim qua `execution` / settings (`test_sandbox_manager.py`). |
