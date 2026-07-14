# Project Memory Registry

## Customer System Understanding (2026-07-14)

- The System Twin primary view is a customer-only topology graph. Omni/Remote Agent
  are control-plane actors and must not be drawn as customer nodes.
- `systemd`, cron, dbus, RPC/NFS helpers, dynamic kernel ports and Omni processes may
  remain in raw evidence/audit but are filtered from the primary architecture view.
- API sequence is contract-first: discover or receive OpenAPI/Swagger, parse metadata
  at the customer/upload boundary, then correlate redacted access-log metadata.
- TCP connections alone are dependency evidence, never HTTP sequence evidence.
- Read-model statuses are `runtime_verified`, `contract_observed`, `missing_contract`,
  and `network_only`; incomplete evidence must remain visible as an unknown/action.
- Canonical details: [`customer-system-understanding.md`](../architecture/customer-system-understanding.md).

**North Star + audit verify log (định kỳ):** [audit-snapshot-2026-05.md](audit-snapshot-2026-05.md)

**Canonical kiến trúc (bám code):** [../vendor/OMNI_PROJECT_CANONICAL.md](../vendor/OMNI_PROJECT_CANONICAL.md)

## Invariants

- **Shadow OS command governance (2026):** default runtime runs in `OMNI_SHADOW_OS_MODE=true`; planner output must route to `SUGGEST_OS_RUNBOOK` with strict command schema (`dry_run_command`, `command`, `rollback_command`, `evidence_refs`). SDK mutate execution path is fail-closed by executor kill-switch. Host-level command execution must be wrapped via `nsenter -t 1 -m -u -i -n -p -- <linux_command>` and audited with `trace_id`, `command_hash`, `host_identity`.
- **Chaos/E2E clean-reset preflight (2026):** Before validating autonomous runtime behavior, run clean-reset in this order: (1) restore target workload baseline state (for `chaos-victim`: DB credential + K8s Secret consistency + healthy pod), (2) purge/recreate Omni Kafka topics backlog, (3) flush Redis autonomy trace/session state, then (4) wait 15 minutes cooldown to drain stale alerts/rebalance noise. Skip this only when explicitly testing backlog/retry semantics.
- **State-machine closure gate (2026):** For chaos credential autonomy loops, `autonomous_feedback_loop` `VERIFIED_SUCCESS` is necessary but not sufficient; loop termination requires explicit K8s workload checks on `chaos-victim`: `kubectl rollout status deployment/chaos-victim -n multi-agent --timeout=60s` **and** deployment condition `Available=True`. If either fails, treat as failed run, restore baseline, and re-run from fault injection.
- **Chaos terminal transition + rollout convergence (2026):** Feedback loop now closes with `TRANSITION_STATE_MACHINE_VERIFIED` (not `VERIFIED_SUCCESS`) and only after deployment rollout convergence (`ready>=desired`, `updated>=desired`, `available>=desired`, `observedGeneration>=generation`). This avoids false-positive closes when old ReplicaSet pods remain Ready during a broken rollout.
- **Chaos credential lab (OrbStack inject, 2026):** With `OMNI_ENV_MODE=prod`, `WorkerSettings` keeps lab chaos **only** when **both** `OMNI_LAB_CHAOS_CREDENTIAL_AUTOFIX_ENABLED=true` and `OMNI_CHAOS_PG_APP_PASSWORD` are set (shipped lab ConfigMap overlay; omit in real prod). `evidence_consumer` merges `chaos_credential_lab_autofix_plan_from_batch` over LLM `k8s_patch_secret` args when credential failure + lab match (planner often emits placeholder `value`). `chaos_credential_lab_autofix` args include `value_source`/`value_source_ref` for precondition gates. `autonomous_feedback_loop`: after secret patch, lab emits chained `k8s_rollout_restart`; second feedback path `chaos_lab_rollout_finalize_verified` polls `check_deployment_rollout_healthy` (ready≥desired; rolling updates no longer require `unavailable_replicas==0`) then `VERIFIED_SUCCESS`. Code: [`deterministic_mutate_from_evidence.py`](../../src/pkg/reasoning/deterministic_mutate_from_evidence.py), [`evidence_consumer.py`](../../src/workers/evidence_consumer.py), [`autonomous_feedback_loop.py`](../../src/workers/autonomous_feedback_loop.py), [`post_verify_deployment_state.py`](../../src/workers/post_verify_deployment_state.py).
- **Planner anti-loop + secret provenance (2026):** `run_agentic_mutate_plan` must enforce hard guard for repeated identical read-only actions (threshold=2 on `tool+args_sha`) and force `mutate|escalate` decision instead of endless discovery. For `k8s_patch_secret`, mutate preconditions require both secret-structure evidence (`secret_ref_confirmed`) and credential provenance (`credential_source_of_truth` via `value_source` + `value_source_ref`), sourced through read-only tools (`k8s_get_pod_secret_refs`, `k8s_get_secret_keys`) that never expose secret values.
- **Post-mutate state-success (2026):** `handle_action_feedback_envelope` does **not** emit `VERIFIED_SUCCESS` on executor `exit_code=0` alone when `OMNI_POST_MUTATE_VERIFY_PLANNER_ENABLED=true` (default). After `OMNI_VERIFY_DELAY_SEC`, `run_verify_probes` builds a fresh Fact Table; Redis `OmniTraceMemory` records an `ActionRecord` with `kind=post_mutate_verify`; `run_post_mutate_state_verify_planner` re-enters `run_agentic_mutate_plan` with `post_mutate_verify` context (`[EXECUTOR_FEEDBACK]` + probe summary). The LLM must emit `phase: done` with evidence-backed `resolution_summary` before `_finalize_feedback_success_verified`. Caps: `OMNI_STATE_VERIFY_MAX_ATTEMPTS` (per-trace state gate cycles), `OMNI_POST_MUTATE_STATE_VERIFY_MAX_STEPS`. Legacy path (SDK-only / no planner gate): set `OMNI_POST_MUTATE_VERIFY_PLANNER_ENABLED=false`. Code: [`autonomous_feedback_loop.py`](../../src/workers/autonomous_feedback_loop.py), [`analyst_agentic_loop.py`](../../src/workers/analyst_agentic_loop.py), [`trace_memory.py`](../../src/workers/memory/trace_memory.py).
- **Integration E2E ReAct (tests):** `tests/integration/test_e2e_autonomous_loop.py` runs `run_agentic_mutate_plan` against `SimulatedClusterState` + `SmartLLMStub` with a **glassbox** `ReActAuditTrail` (hashed prompts, tool I/O). `run_agentic_mutate_plan` does **not** execute mutate in-process — a full patch → verify → `phase: done` scenario uses **two** planner calls and a simulated executor step; see `tests/integration/README.md`.
- **Planner InitialSymptom + sole evaluator (2026):** `evidence_consumer` builds `InitialSymptom` from the batch (`initial_symptom_from_evidence_batch`) and passes it into `run_agentic_mutate_plan`; Redis-backed `OmniTraceMemory` stores `initial_symptom` and renders `<INITIAL_SYMPTOM>` inside `<TRACE_MEMORY>`. Each ReAct round **reloads** trace memory from Redis before the LLM call. `OMNI_PLANNER_LLM_SOLE_EVALUATOR=true` disables Python regex-based planner hints (`_general_credential_failure_hint`, `_broken_spec_first_round_instruction`); archivist recall remains. `OMNI_TRACE_MEMORY_TOOL_OUTPUT_MAX_CHARS` raises the cap for readonly `ActionRecord` text in history. Code: [`initial_symptom.py`](../../src/workers/memory/initial_symptom.py), [`trace_memory.py`](../../src/workers/memory/trace_memory.py), [`analyst_agentic_loop.py`](../../src/workers/analyst_agentic_loop.py).
- **Label schema (2026):** end-to-end keys in [`docs/vendor/OMNI_LABEL_SCHEMA.md`](../vendor/OMNI_LABEL_SCHEMA.md) — `trace_id` / `omni.io/incident-id` correlation, alert `omni_verify_required` gates SDK verify, Resolution DNA on successful `action_experience` upsert (`omni.io/root-cause-id`, `omni.io/resolution-tool`, …). Parser: [`src/pkg/reasoning/alert_identity.py`](../../src/pkg/reasoning/alert_identity.py).
- **Diagnostic Policy (2026):** deterministic **`INV_*`** gates in [`diagnostic_policy.py`](../src/pkg/reasoning/diagnostic_policy.py) — LLM cannot override. **`INV_NO_RESTART_ON_BROKEN_SPEC`** blocks `k8s_rollout_restart` when evidence shows missing ConfigMap/Secret/mount-class failure; **`INV_READ_BEFORE_MUTATE_DEFER`** requires at least one read-only discovery step (ReAct tool round when `OMNI_DIAGNOSTIC_REACT_ENABLED`, or equivalent evidence in the batch); **`INV_NAMESPACE_ISOLATION`** enforces `autonomous_allowed_namespaces` (security log + Telegram on violation). Enforced in [`evidence_consumer.py`](../src/workers/evidence_consumer.py) after proof-of-fault, before `EXECUTE_MUTATE`. Structured UX: `reasoning_chain` / `verdict` / `thought_process` on `SUGGEST_REMEDIATION` ([`omni_actions_remediation.py`](../src/workers/omni_actions_remediation.py)). Spec: [diagnostic-policy-spec.md](diagnostic-policy-spec.md). Agentic: Fact Table + optional ReAct in [`analyst_agentic_loop.py`](../src/workers/analyst_agentic_loop.py); blind matrix miss: `infer_blind_proof_lane_hint` + `resolve_proof_lane(..., blind_lane_hint=)` in [`incident_matrix_profile.py`](../src/pkg/reasoning/incident_matrix_profile.py). RAG: hints may include `diagnostic_pattern` from matrix row ([`gate.py`](../src/pkg/rag/gate.py) `normalize_rag_query`).
- **Action feedback Kafka topic (split):** execution outcomes are published to **`omni-action-feedback`** (settings `kafka_topic_action_feedback`); consumed by **`omni-analyst`** (`kafka_action_feedback_loop`). Not `omni-results` — see canonical doc.
- `EXECUTE_MUTATE` only executes mutate-capable tools; read/query tools must route to `SUGGEST_REMEDIATION`.
- Mutate decisions are fail-closed in `prod` and must keep `trace_id` + auditable `reason_code`.
- Planner output cannot override Proof-of-Fault controls (`_proof_of_fault_gate`). **Three lanes** (`OMNI_PROOF_LANE_ENABLED`, default true): **`resource`** — baseline sigma (`dr`/z) + Redis observation window; **`state`** — deterministic K8s/container failure signals, fast-track without sigma; **`app_log`** — optional Loki sustained 5xx when sigma is flat. Sigma log bypass (`OMNI_SIGMA_LOG_BYPASS_ENABLED` + Loki) applies only for **`app_log`** lane when matrix/RAG mark **API/Web**, namespace allowlist, and pod identity exist — never bypass on Loki failure (`ERR_REA_LOG_SOURCE_UNAVAILABLE` → escalate, no mutate). Spec: [incident-evidence-three-lanes.md](incident-evidence-three-lanes.md), [sigma-log-bypass-spec.md](sigma-log-bypass-spec.md).
- Runtime/app config must not ship embedded credentials; DSN defaults stay placeholder-only and secret-injected at runtime.
- Grafana provisioning for Omni monitoring includes `Omni Ops`, `Omni Security`, `Omni Learning`, `Omni Pod Resources`, `Omni Node Resources`, and **`Omni SLO minimum`** (`omni_slo_minimum.json` — proactive duration p95, requires_human rate, worker lag / redis stream backlog). Loki datasource: **derived fields** on JSON `trace_id` and bracket-prefixed trace lines → Tempo explore; Tempo **tracesToLogs** → Loki (align OTLP trace IDs with log `trace_id` when both enabled).
- **SDK verify matrix (2026):** `OMNI_SDK_VERIFY_OPTIONAL_PROBES` allows INCONCLUSIVE on listed Prom probes without failing verify. `OMNI_EXPERIENCE_REQUIRES_SDK_VERIFY` (default true) blocks RAG upsert on legacy finalize without SDK-verified path.
- Advanced self-learning tiers must be zero-impact by default: `OMNI_MULTI_HYPOTHESIS_ENABLED=false`, `OMNI_DEEP_PROBE_ORCHESTRATION_ENABLED=false`, `OMNI_KNOWLEDGE_DRAFT_ENABLED=false`, `OMNI_AUTODOC_GIT_PUSH_ENABLED=false`.
- Incident training execution must be registry-driven (`config/incident_training_matrix.yaml`) and not hardcoded in scattered shell branches.
- Chaos / RAG self-learning lab (banking-safe path B): do not auto-ingest Redis shadow artifacts (`omni:selflearn:shadow:*`) into PGVector; gold dataset for vector ingest only after human **VERIFIED_SUCCESS** and a separate ingest step (`docs/reports/chaos-rag-selflearn-export-ingest.md`).
- Sprint A lab: keep `OMNI_AUTODOC_GIT_PUSH_ENABLED=false`; no automated `git push` for `docs/vendor/knownbase.md` from workers — updates via human PR only.

## AlertPipelineMemory (2026 — alert flow autonomy)

Implemented behavior to lock in:

- **Matrix matching (`diagnostic_mapping._row_matches`):** Label predicates (`labels_alertname`, `labels_domain`, `labels_workload`, `labels_reason_pattern`) apply **only when that key exists** on the parsed Prometheus `labels` in `canonical_query` JSON. Missing keys **waive** the predicate so rows can still match via `error_hint_pattern` / `canonical_query_pattern` — avoids specific rows never firing when alerts lack full labels. **Priority** remains ascending (lower number first); see `config/diagnostic_matrix.yaml` header.
- **Alert → `AnomalyEvent` (`alert_to_event`):** Prometheus path uses **sorted JSON** for `canonical_query` (stable), stringified labels/annotations, **identity bits** (pod/deployment/container) in `error_hint`, optional `trigger_promql` from annotations or payload.
- **RAG before LLM:** `filter_evidence_for_rag` adds `probes=`, `symptom_group`, `layer`; `_hints_from_evidence_batch` merges batch `alert_rule` / `symptom_group` and parses `rule:` / `symptom_group:` from sanitized text; `normalize_rag_query` prefixes **`symptom_group=`** when present in hints.
- **Contradiction / escalate:** `llm_contradicts_sdk_facts` ignores contradiction when LLM text is **hedge-only** (maybe/possibly/…) **without** crash/CPU/false-alarm terms — reduces spurious `SDK_CONTRADICTION` escalations.
- **Observability doc:** [rag-gate-observability.md](rag-gate-observability.md) — grep patterns + env for RAG gate tuning.
- **Tests:** `tests/test_alert_to_event.py`, `tests/test_alert_pipeline_golden.py`, extended `test_filter_evidence_for_rag`, `test_evidence_anchor_sre_output`.

## OmniStateMachineContrast (2026 — trust SM khi lệch)

**Invariant (nghiệp vụ):** Khi `compare_alert_claim_to_sdk_state` trả chuỗi (không `None`), pipeline đã thấy **mâu thuẫn rõ** giữa tuyên bố alert (workload CPU/mem) và **state machine** từ K8s (`PodStatus` / `PodMetrics` trong phạm vi batch). **Ưu tiên tin state machine** cho quyết định contrast; alert/Prometheus được xử lý như **đáng kiểm tra** (stale, expr, label, scrape lag) — không suy diễn ngược “API sai” trừ khi có bằng chứng khác.

**Kích hoạt (tất cả phải qua):** `symptom_group=workload_resource` trong batch; không có `labels.reason` từ snippet; `k8s_clinical_pod_status` không làm vô hiệu so CPU (phase/signal không “invalidate stale sample”); có `k8s_clinical_pod_metrics` với `result` ∈ {`PASSED`,`INCONCLUSIVE`}; nếu `INCONCLUSIVE` thì không 404/podmetrics-not-found và có `containers`; mọi CPU container trong scope **effectively zero**; khi đó mới emit narrative + `build_contrast_operator_telegram_body` / `build_contrast_diagnosis_for_action` → `SUGGEST_REMEDIATION` source `STATE_MACHINE_CONTRAST`. Code: [`src/workers/alert_sdk_truth_compare.py`](../../src/workers/alert_sdk_truth_compare.py), gọi tại [`src/workers/evidence_consumer.py`](../../src/workers/evidence_consumer.py). Locale digest: `OMNI_OPERATOR_DIGEST_LOCALE` (`en`|`vi`|`both`).

**Đọc dự án theo lát (ghi nhớ, không cần một lần):** (S1) gateway webhook + trace → (S2) `omni_worker` stream_consumer + `diagnostic_dispatcher` publish evidence → (S3) `evidence_consumer` + `compare_alert_claim_to_sdk_state` ← **mốc contrast** → (S4) advisory/telegram/CRAT → (S5) `kafka_actions_consumer` suggest vs mutate → (S6) `kafka_action_feedback_loop` / `autonomous_feedback_loop`. Death-loop lab: `scripts/e2e_death_loop_lab_complete.sh` + `scripts/e2e_collect_trace_evidence.sh` (`count_command_feedback_ingested`).

## LabVsRealAlertTesting

- **Trace audit `gw-prom-f58ffe43e85e` (2026-04-09, lab nginx missing ConfigMap) — CLOSED 2026-04-11:** Full write-up: [`trace-audit-gw-prom-f58ffe43e85e.md`](trace-audit-gw-prom-f58ffe43e85e.md). Loki `query_range` (3-day window, corrected epoch): **33 rows, 3 streams** (prober=11, analyst=19, executor=3). Full pipeline: INGESTED → CONTEXT_READY(×3) → DIAGNOSED → diag_batch_flush → rag_gate_hit → rag_hints_buffered(`proof_lane=resource, broken_spec=True`) → PLAN_EMITTED(seq 8) → agentic_loop(ERR_REA_SCHEMA_VIOLATION ×4) → agentic_mutate_plan_fail → agentic_mutate_fallback(`k8s_rollout_restart, reason=planner_unavailable`) → action_emitted(`SUGGEST_REMEDIATION, source=PROOF_OF_FAULT_GATE`) → PLAN_EMITTED(seq 9) → executor `SUGGEST_REMEDIATION (no execute)`.
  **Verdict: two defects on initial commit, both FIXED 2026-04-10.** (1) `k8s_describe_resource` schema missing ConfigMap/Secret → planner ERR_REA_SCHEMA_VIOLATION; (2) fallback `not plan` branch unconditionally used `k8s_rollout_restart`, not checking broken-spec → proof gate hit sigma block (`ERR_REA_SIGMA_GATE_BLOCKED`) before invariant gate. Also fixed 2026-04-11: fallback broken-spec path now sets `blind_lane_eff="state"` so proof gate fast-tracks without sigma (`src/workers/evidence_consumer.py` `fallback_lane_override`). Test: `tests/test_configmap_remediation.py::test_fallback_lane_is_state_for_broken_spec_cm`. Final action SUGGEST_REMEDIATION is still expected (per `INV_NO_RESTART_ON_BROKEN_SPEC` / FailurePatterns) until `OMNI_ENV_MODE=lab` executor is verified to run `k8s_create_or_patch_configmap`.

- **Inject self-remediation (`scripts/inject_self_remediation_alerts.py`):** chạy **in-cluster** (`kubectl exec … deploy/omni-prober -- python3 /app/scripts/inject_self_remediation_alerts.py --only <rbac|configmap|oom>`) để tránh lỗi HTTP khi port-forward từ host; `trace_id` pipeline = giá trị gateway trả về (header `X-Omni-Trace-Id` hợp lệ được honor). Gom log/Loki theo đúng chuỗi đó — inject **từng** alert nếu cần quan sát analyst kịp (consumer serial).
- **Gateway E2E mặc định** (`scripts/gateway_alert_loki_verify.sh`, payload nginx HighCPU): chứng minh **split topology + Kafka + trace_id + probe + evidence batch**. Khi PodMetrics/SDK cho thấy CPU thấp trong khi alert mô tả “nóng”, pipeline đi **`STATE_MACHINE_CONTRAST`** → **trust state machine** (snapshot kubelet/Metrics API trong phạm vi probe); coi **firing alert là đáng nghi** (cảnh báo giả / stale / mismatch selector–series) cho đến khi đối soát thêm trên Prometheus/rule. **Không** dùng thuật ngữ thống kê “false negative” ở đây — tránh nhầm với “bỏ sót sự cố”.
- **Hạn chế:** case lab **không** tái hiện **incident production thật** (load, race, outage một phần). Smoke lab ≠ chứng minh chất lượng chẩn đoán khi **alert đúng** (TRUE positive path) hoặc khi cần **mutate**.
- **Kế hoạch test luồng alert “thực”** (staging / replay / soak): [alert-flow-realistic-test-plan.md](alert-flow-realistic-test-plan.md) — scaffold: [PHASE0_CHECKLIST.md](alert-flow-realistic/PHASE0_CHECKLIST.md), [scripts/alert_flow_realistic/README.md](../../scripts/alert_flow_realistic/README.md), [artifact_template.json](../../reports/alert-flow-realistic/artifact_template.json).

- **E2E full luồng DoD (trust but verify + death loop, 2026-05):** Không PASS chỉ vì một `grep`. Checklist: [e2e_full_flow_evidence_checklist.md](../runbooks/e2e_full_flow_evidence_checklist.md). Kịch bản **A** (gateway smoke), **B** (`e2e_one_alert_full_advisory_path.sh`), **C** (đếm `omni-action-feedback` / terminal — `scripts/e2e_collect_trace_evidence.sh`). Artifact layout (không commit secret): [e2e-artifacts/README.md](e2e-artifacts/README.md). Cluster runbook: [e2e_cluster_after_deploy.md](../runbooks/e2e_cluster_after_deploy.md).
- **CRAT 4-phase verify (`scripts/verify_e2e_crat_pipeline.py`):** `XADD` vào đúng Redis mà `omni-siem-bridge` đọc (`finguard-customer` theo deploy mặc định); script mặc định `E2E_SIEM_REDIS_NAMESPACE=finguard-customer,smart-siem`. `build_anomaly_event_from_alert_payload` fallback `trace_id` từ `alerts[0].labels.trace_id` khi envelope thiếu top-level — tránh evidence/advisory lệch `fg-*`. Phase 1 script chỉ PASS khi message Kafka **correlated** đúng `trace_id` (không khớp message lạ từ backlog).

## FailurePatterns

- **`omni-worker-configmap.yaml` YAML drift:** Merge/paste errors can leave a quoted URL line without a key under `data:` (seen around `OMNI_VLLM_EMBED_URL`) → `kubectl apply` fails (`did not find expected key`) before rollout. Fix: single-line `KEY: "value"`; validate with `./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker-configmap.yaml --dry-run=client`.

- **E2E gateway script (`scripts/gateway_alert_loki_verify.sh`)** depends on **`kubectl exec`** into `E2E_EXEC_DEPLOY` (default `omni-prober`). **`NS` is required** (no default in script; lab: `NS=multi-agent` via Makefile or `export NS`). Some cluster runtimes return `container not found` during exec even when pods are Ready — treat as **infra_blocker** (see `docs/vendor/knownbase.md`). **`scripts/e2e_incident_matrix.sh`** must propagate the gateway script exit code (capture `|| rc=$?` before `_restore_nginx`) or the matrix run incorrectly reports **passed** when strict assert failed.
- **Diagnostic policy path:** `nginx_waiting_fault` lab may emit **`SUGGEST_REMEDIATION`** with **`DIAGNOSTIC_INVARIANT_GATE`** / `INV_NO_RESTART_ON_BROKEN_SPEC` instead of **`EXECUTE_MUTATE`** when evidence shows broken ConfigMap/Secret class failure — expected after invariant rollout; optional grep: `E2E_ASSERT_DIAGNOSTIC_POLICY=1` in `gateway_alert_loki_verify.sh`. With current code (post 2026-04-10 fixes), the intended path for FailedMount+missing CM is: LLM planner describes ConfigMap → proposes `k8s_create_or_patch_configmap` with `lane_hint=state` → proof gate fast-tracks → EXECUTE_MUTATE; fallback (LLM unavailable) also selects `k8s_create_or_patch_configmap` and forces `blind_lane_eff=state`.
- **Broken-spec fallback lane override (2026-04-11):** When `plan=None` (LLM unavailable) and `evidence_suggests_broken_spec + CM name found`, `_emit_agentic_mutate_if_any` sets `fallback_lane_override="state"` and merges it into `blind_lane_eff` before calling `_proof_of_fault_gate`. Without this, `proof_lane=resource` from the matrix causes `ERR_REA_SIGMA_GATE_BLOCKED` (no metric spike for a missing ConfigMap). Fix in `src/workers/evidence_consumer.py`; test `test_fallback_lane_is_state_for_broken_spec_cm`.
- Classifier misroute can happen when broad regex rows run before label-constrained rows; **mitigated** for sparse labels via predicate waiver + explicit priority ordering (see AlertPipelineMemory).
- Planner can emit read-only/hallucinated tools even when JSON shape is valid.
- Single metric spikes are noisy; windowed sigma checks are required before mutation.
- Strict proactive audit can fail in low-noise lab windows (`sigma_gate_ok=false`) even when rollout and contract tests pass.
- Strict trace-stage checks can be timing-sensitive under split topology/log propagation.
- Dashboard drift appears when ConfigMap payload and JSON source files are not synchronized from one canonical set.
- Full matrix can pass while strict audit still fails if lab noise is too low for sigma evidence (`dr=0`, `z=0`) or trace propagation races under proactive checks.
- Missing **Registry** (trace_id ↔ scenario_id) at Matrix run time forces log archaeology and corrupts Learning Delta / labels.
- Redis shadow TTL (24h `setex`) can expire before reviewer export — artifact loss; monitor `ttl_remaining_sec` in exporter output (`scripts/omni_redis_shadow_jsonl_exporter.py`).
- Learning Delta invalid if PGVector baseline or embed model changes between round 1 and round 2.

## ReasonCodes

- Semantic/channel: `ERR_SEM_CHANNEL_MISMATCH`, `ERR_SEM_INVALID_TOOL_TAXONOMY`.
- Governance: `ERR_GOV_NS_OUT_OF_BOUNDS`, `ERR_GOV_UNAUTHORIZED_MUTATION`, `ERR_GOV_ENV_PROD_STRICT`.
- Reasoning/evidence: `ERR_REA_NO_PHYSICAL_PROOF`, `ERR_REA_SIGMA_GATE_BLOCKED`, `ERR_REA_LOG_SOURCE_UNAVAILABLE` (Loki unavailable during sigma log bypass), `ERR_REA_SCHEMA_VIOLATION`, `ERR_REA_HALLUCINATION_DETECTED`.
- Terminal: `SUCCESS_VERIFIED_EVIDENCE`, `ESC_TIMEOUT_TOMBSTONE`, `ESC_MAX_ATTEMPTS_EXCEEDED`.

## Guardrails

- Keep mutate/read-only taxonomy explicit in runtime constants and CI gates.
- Keep classifier regression gate for `ProbeFailureLab` not mapping to `ollama_500_context`.
- Documentation gate blocks incomplete phase records.
- Keep `gitleaks` critical gate for working tree (`--no-git`) and run history scan as separate governance audit target.
- Always classify runtime verify failures explicitly as `infra_blocker` or `logic_blocker` before release messaging.
- Keep non-impact gates enabled in CI (`validate_nonimpact_guards_gate.py`, `validate_learning_loop_gate.py`) before any self-learning tier promotion.

## TechnicalDebt

- **Multi-cluster / multi-platform in one Omni:** not implemented — one Omni stack targets one Kubernetes API server; no in-repo abstraction for multiple kube contexts or per-cluster routing in a single process. Operational workaround: **deploy Omni per cluster**; future work if needed: fleet registry + `cluster_id` on alerts/actions + multi-client executor (see canonical §9).
- **Remediation plan (excluding fleet):** [tech-debt-remediation-plan.md](tech-debt-remediation-plan.md) — waves A–D (RBAC, Ollama/Redis/monitor reliability, classifier/proactive, doc hygiene).

## CrossPhaseConstraints

- Any change touching mutate/classifier/planner must update tests and gates together.
- Every phase report must include `What Changed in System Behavior` and `Memory Applied`.
- Any detected historical secret requires key rotation first, then explicit approval before history rewrite actions.

## Sprint log (2026-05-12) — Qwen 3.6 + LLM routing + trace orchestrator scaffold

- **Defaults:** `WorkerSettings` chat/reasoning/heavy/helper → **`qwen3.6`**; fallbacks + scripts (`init_ollama`, `mvp_*`, `test_live_agent`) + benchmark default + Smart-SIEM `worker-deployments` + UI onboarding/SIEM overview strings; `CLAUDE.md` / `CODEBASE.md` / `knownbase` examples aligned.
- **LLM contract:** `LLMCallKind`, `VLLMClient.chat_plain` / `chat_structured`, optional `llm_call_kind` debug on `chat`; `LlmClient` protocol updated. Call sites: `proactive_observer._parse_fallback_tool_call`, `autonomous_decider._tick_react`, `agentic_slow_path`, `analyst_agentic_loop` (blind lane → `chat_plain`, planner/post-verify → `chat_structured`).
- **Trace orchestrator:** new `src/pkg/trace_orchestrator/` (Redis `omni:trace_orchestrator:{trace_id}`, candidate helpers); `evidence_consumer` initializes state and enqueues `playbook:{id}` on matcher hit; `docs/design/trace-orchestrator.md`; `tests/test_trace_orchestrator.py`, `tests/llm_mock_compat.py`.
- **Verify:** `pytest tests/ --ignore=tests/integration --ignore=tests/real_services` — green. `tests/real_services/*` needs live stack. `make autonomy-gate` failed `secret-gate` (gitleaks on `tests/test_observability_normalize_pure.py` test string — pre-existing).
- **Coverage (scoped `--cov` on touched paths):** `vllm_client` ~94%; trace_orchestrator submodules ~80–91%. Full-repo 90% not claimed for this diff alone.
- **Docker:** `make docker-worker`, `make docker-gateway` — OK.
- **Cluster E2E:** not run this session.

### Backend verify harness + LLM/Gateway SLI (2026-05-12)

- **Exporter:** `omni_llm_ttft_seconds`, `omni_llm_client_completion_seconds`, `omni_llm_completion_tokens_total` — [`src/workers/metrics_exporter.py`](../../src/workers/metrics_exporter.py) `observe_llm_client_sli`.
- **VLLMClient:** streamed completions when `OMNI_VLLM_STREAM_FOR_SLI=true` (default **false** in library for unit-test mocks; worker ConfigMap sets **true**); `OMNI_LLM_SLI_METRICS_ENABLED` — [`src/llm/vllm_client.py`](../../src/llm/vllm_client.py).
- **Gateway:** `omni_gateway_circuit_open` gauge on Redis CB read — [`src/gateway/api.py`](../../src/gateway/api.py).
- **Redis stream XLEN:** `OMNI_METRICS_REDIS_STREAM_KEYS` comma list → `observability_metrics_loop` — [`src/workers/settings.py`](../../src/workers/settings.py), [`src/workers/omni_worker.py`](../../src/workers/omni_worker.py); lab example `stream:actionable_incidents`.
- **Scripts:** [`scripts/wait_omni_consumer_ready.py`](../../scripts/wait_omni_consumer_ready.py), [`scripts/omni_backend_verify.py`](../../scripts/omni_backend_verify.py) — POST Gateway webhook (HMAC when secret set), optional `/metrics` CB scrape, trace-scoped DLQ poll (not global DLQ count).
- **NetPol (lab):** [`k8s/network-policies/omni-gateway-netpol-ingress-multi-agent.yaml`](../../k8s/network-policies/omni-gateway-netpol-ingress-multi-agent.yaml) — replaces broken `ingress: - from: []` rule (blocked all in-cluster sources).
- **Analyst Service:** [`k8s/services/omni-analyst-service.yaml`](../../k8s/services/omni-analyst-service.yaml) — ClusterIP `:8090/:9090` for `omni-analyst.*` DNS (required for `/readyz` wait).
- **Job:** [`k8s/jobs/omni-backend-verify.yaml`](../../k8s/jobs/omni-backend-verify.yaml) — `ttlSecondsAfterFinished: 60`, optional `omni-gateway-secret` envFrom.
- **Makefile:** `wait-omni-consumer-ready`, `backend-verify-local`, `backend-verify-job-infra`, `backend-verify-job-apply`, `backend-verify-job-run`.

**VERIFY SUMMARY (session slice)**

| Gate | Result |
|------|--------|
| Omni pytest unit (`tests/` excl. integration + `tests/real_services`) | PASS |
| `make hitl-gate` | PASS |
| Cluster smoke (`kubectl get pods -n multi-agent`) | Analyst/Gateway Running |
| `k8s/network-policies/omni-gateway-netpol-ingress-multi-agent.yaml` | Applied — ingress peers: same-ns `podSelector: {}` + Traefik |
| `k8s/services/omni-analyst-service.yaml` | Applied — DNS `omni-analyst.multi-agent.svc.cluster.local:8090` |
| Gateway URLs | Service **`ClusterIP` port 80 → targetPort 8000** (scripts/job must use **:80** or omit port; `:8000` fails) |
| `make docker-worker` + Job `omni-backend-verify` | **PASS** — webhook 200, trace DLQ clean |
| `make docker-gateway` + rollout omni-gateway | `/metrics` exposes **`omni_gateway_circuit_open 0.0`** |
| `make autonomy-gate` | Not run (heavy; prior sprint note: secret-gate noise) |

## Build / deploy / debug (2026-05-12) — worker ConfigMap + cluster rollout

- **Deploy blocker:** `k8s/deployments/omni-worker-configmap.yaml` YAML không hợp lệ — `OMNI_VLLM_EMBED_URL` thiếu value trên cùng dòng, URL rơi xuống dòng orphan → `kubectl apply` lỗi `yaml: line 88: did not find expected key`.
- **Fix:** Gộp `OMNI_VLLM_EMBED_URL: "http://host.orb.internal:11434/v1"` một dòng; xóa dòng quoted lạc.
- **Build:** `make docker-worker` → image `multi-agent-system:latest` OK (OrbStack/docker local).
- **Deploy:** `make deploy-worker` OK sau fix — rollouts `omni-prober`, `omni-analyst`, `omni-core`, `omni-executor` trong timeout 180s.
- **Logs (debug):** Executor `worker_role=executor`, consumer `omni-actions`, audit-only `SUGGEST_REMEDIATION`; WARNING heartbeat/rebalance Kafka trong lúc rollout là bình thường. Analyst khởi động evidence + action-feedback + KPI collector (có LeaveGroup khi pod terminate trong rollout).
- **Test:** `make auto-execute-gate` PASS (10 tests).
- **Tài liệu:** Preflight ConfigMap — [../runbooks/e2e_cluster_after_deploy.md](../runbooks/e2e_cluster_after_deploy.md); policy auto-execute — [../runbooks/auto-execute-policy-matrix.md](../runbooks/auto-execute-policy-matrix.md).
