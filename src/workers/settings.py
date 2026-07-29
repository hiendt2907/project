"""Omni-worker env (Pydantic Settings).

Runtime values come from environment (OMNI_* prefix) and Kubernetes ConfigMap/Secret.
Defaults below are fallbacks when unset — override via env, not by editing literals here for production.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _coerce_http_base_url(v: object) -> object:
    if v is None or not isinstance(v, str):
        return v
    s = v.strip()
    if not s:
        return v
    if "://" not in s:
        return f"http://{s}"
    return s


def default_prometheus_http_base() -> str:
    """HTTP base URL matching ``WorkerSettings`` defaults after scheme coercion (for docs / fallback when no ctx)."""
    raw = _coerce_http_base_url("prometheus.monitor.svc.cluster.local:9090")
    return raw if isinstance(raw, str) else "http://prometheus.monitor.svc.cluster.local:9090"


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OMNI_",
        extra="ignore",
        populate_by_name=True,
    )

    @model_validator(mode="after")
    def _god_mode_implies_lab(self) -> "WorkerSettings":
        if self.god_mode:
            self.lab_unchained = True
        if self.env_mode == "prod":
            # Prod is fail-closed: never allow lab/god bypass flags.
            # Cluster scope is operator-controlled: set OMNI_CLUSTER_FULL_ACCESS=false for least-privilege;
            # leave true (Field default) when the executor SA is intentionally cluster-admin.
            self.god_mode = False
            self.lab_unchained = False
            self.proactive_fallback_bypass_policy_in_god_mode = False
            self.omni_unrestricted_tool_execution = False
            self.ingest_secrets_raw = False
            # Chaos credential lab (OrbStack inject): allow only when explicitly enabled **and**
            # OMNI_CHAOS_PG_APP_PASSWORD is set in the overlay — otherwise strip (fail-closed).
            lab_chaos = bool(self.lab_chaos_credential_autofix_enabled)
            pwd_chaos = self.chaos_pg_app_password.get_secret_value().strip() if self.chaos_pg_app_password else ""
            if not lab_chaos or not pwd_chaos:
                self.lab_chaos_credential_autofix_enabled = False
                self.chaos_pg_app_password = SecretStr("")
        return self

    @model_validator(mode="after")
    def _proactive_timeouts_sane(self) -> "WorkerSettings":
        if self.proactive_event_timeout_sec <= self.proactive_tool_timeout_sec:
            raise ValueError(
                "proactive_event_timeout_sec must be greater than proactive_tool_timeout_sec "
                "(OMNI_PROACTIVE_EVENT_TIMEOUT_SEC / OMNI_PROACTIVE_TOOL_TIMEOUT_SEC)"
            )
        return self

    @property
    def victoria_metrics_url(self) -> str:
        """Tương thích đọc cũ — cùng giá trị với ``prometheus_url`` (VictoriaMetrics đã thay bằng Prometheus)."""
        return self.prometheus_url

    redis_url: str = Field(default="redis://redis:6379/0")
    redis_sentinel_hosts: str = Field(
        default="",
        description="Comma-separated host:26379 for Redis Sentinel; empty = OMNI_REDIS_URL standalone only.",
    )
    redis_sentinel_master_name: str = Field(
        default="mymaster",
        description="Redis Sentinel master name (must match sentinel config).",
    )

    kafka_bootstrap_servers: str = Field(
        default="kafka:9092",
        description="Kafka bootstrap (PLAINTEXT in-cluster).",
    )
    kafka_topic_alerts: str = Field(
        default="omni-alerts",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_ALERTS", "OMNI_STREAM_INBOUND"),
        description="Ingress alerts + telegram callback (former events:inbound).",
    )
    kafka_topic_dlq: str = Field(
        default="omni-dlq",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_DLQ", "OMNI_STREAM_DLQ"),
    )
    kafka_topic_proactive_incidents: str = Field(
        default="omni-proactive-incidents",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_PROACTIVE_INCIDENTS", "OMNI_STREAM_INCIDENTS_PROACTIVE"),
    )
    kafka_topic_audit_sandbox: str = Field(
        default="omni-audit-sandbox",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_AUDIT_SANDBOX", "OMNI_AUDIT_SANDBOX_STREAM"),
    )
    kafka_topic_audit_proactive: str = Field(
        default="omni-audit-proactive",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_AUDIT_PROACTIVE", "OMNI_AUDIT_PROACTIVE_STREAM"),
    )
    kafka_topic_audit_agent: str = Field(
        default="omni-audit-agent",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_AUDIT_AGENT", "OMNI_AUDIT_AGENT_STREAM"),
    )
    kafka_topic_diagnostic_evidence: str = Field(
        default="omni-diagnostic-evidence",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_DIAGNOSTIC_EVIDENCE", "OMNI_DIAGNOSTIC_EVIDENCE_STREAM"),
    )
    kafka_topic_discovery_evidence: str = Field(
        default="omni-discovery-evidence",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_DISCOVERY_EVIDENCE"),
        description="Onboarding worker input: process_list/port_scan/service_topology/doc-snapshot probes.",
    )
    kafka_topic_knowledge_evidence: str = Field(
        default="omni-knowledge-evidence",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_KNOWLEDGE_EVIDENCE"),
        description="Knowledge worker input: METRIC_SAMPLE/LOG_SAMPLE/DISCOVERY/CHANGE_DETECTED from remote agents.",
    )
    kafka_topic_siem_chains: str = Field(
        default="omni-siem-chains",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_SIEM_CHAINS"),
        description="Smart-SIEM correlation chains from brain-go graph correlator.",
    )
    kafka_topic_tool_audit: str = Field(
        default="omni-tool-audit",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_TOOL_AUDIT"),
        description="Mutating tool audit (former events:audit).",
    )
    kafka_topic_actions: str = Field(
        default="omni-actions",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_ACTIONS"),
        description="Executor service: mutation jobs (JSON envelope).",
    )
    kafka_topic_action_feedback: str = Field(
        default="omni-action-feedback",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_ACTION_FEEDBACK"),
        description="Executor → Analyst: mutate result (stdout/stderr/exit_code).",
    )
    kafka_topic_hitl_pending: str = Field(
        default="omni-hitl-pending",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_HITL_PENDING"),
        description="HITL gate: actions awaiting human approval before executor dispatch.",
    )
    kafka_topic_audit_chain: str = Field(
        default="omni-audit-chain",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_AUDIT_CHAIN"),
        description="CRAT tamper-evident audit chain (SOX §404, PCI-DSS v4.0). cleanup.policy=compact, infinite retention.",
    )
    kafka_topic_advisory_suggestions: str = Field(
        default="omni-advisory-suggestions",
        validation_alias=AliasChoices("OMNI_KAFKA_TOPIC_ADVISORY_SUGGESTIONS"),
        description="Advisory Mode: durable log of suggestions sent to Telegram + operator "
                    "acknowledgment decisions (pending_ack -> acknowledged). Not a mutation-"
                    "approval gate (that is kafka_topic_hitl_pending, disabled in Advisory Mode).",
    )
    omni_gateway_internal_url: str = Field(
        default="http://omni-gateway.multi-agent.svc.cluster.local:80",
        validation_alias=AliasChoices("OMNI_GATEWAY_INTERNAL_URL"),
        description="Phase 4 (0-6 roadmap): in-cluster gateway URL for the worker's "
                    "auto-recovery dispatch bridge (diagnosis_loop -> durable enqueue). "
                    "In-cluster service DNS, not the ai-agent.local ingress hostname.",
    )
    omni_gateway_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("OMNI_GATEWAY_API_KEY"),
        description="Phase 4: worker's own admin credential for calling the gateway's "
                    "durable-enqueue endpoint internally. Same master key the gateway "
                    "itself accepts (backward-compat admin auth) — empty disables "
                    "auto-dispatch (fail-closed, not a silent no-op).",
    )
    omni_auto_execute_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_AUTO_EXECUTE_ENABLED"),
        description="Lab: EXECUTE_MUTATE runs after Pre-apply without Telegram/Redis confirm.",
    )
    omni_playbook_first: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_PLAYBOOK_FIRST"),
        description="L4 dual-run: mutate call match được PlaybookSpec → EXECUTE_PLAYBOOK (engine gate đầy đủ); không match → EXECUTE_MUTATE cũ.",
    )
    # Graduated autonomy tier (MASTER_PLAN §3). Default fail-closed shadow. Rỗng =
    # dẫn xuất từ omni_auto_execute_enabled. Runtime source-of-truth = Postgres(cache).
    omni_autonomy_tier: str = Field(
        default="",
        validation_alias=AliasChoices("OMNI_AUTONOMY_TIER"),
        description=(
            "SRE-Autonomous mode: shadow|minimal|autonomous (alias: shadow|assist|auto). "
            "shadow=observe-only (mọi mutate SUGGEST); minimal=auto chỉ lỗi cơ bản từ "
            "RAG/deterministic ReAct (origin tin cậy, risk LOW); autonomous=auto cả LLM "
            "ReAct (LOW+MEDIUM, HIGH→HITL). Rỗng → derive từ omni_auto_execute_enabled."
        ),
    )
    # Readiness gating (CHỈ hiển thị — không tự nhảy tier, MASTER_PLAN §5).
    omni_tier_min_days_shadow: int = Field(
        default=90,
        validation_alias=AliasChoices("OMNI_TIER_MIN_DAYS_SHADOW"),
    )
    omni_tier_min_days_assist: int = Field(
        default=270,
        validation_alias=AliasChoices("OMNI_TIER_MIN_DAYS_ASSIST"),
    )
    omni_tier_min_advisories: int = Field(
        default=50,
        validation_alias=AliasChoices("OMNI_TIER_MIN_ADVISORIES"),
    )
    omni_tier_min_graduated_playbooks: int = Field(
        default=1,
        ge=0,
        validation_alias=AliasChoices("OMNI_TIER_MIN_GRADUATED_PLAYBOOKS"),
        description=(
            "Số playbook GRADUATED tối thiểu trước khi đề xuất nâng tier. Acceptance cao "
            "chứng minh chẩn đoán đúng, không chứng minh đã có quy trình lặp lại được."
        ),
    )
    omni_tier_shadow_assist_wilson: float = Field(
        default=0.80,
        validation_alias=AliasChoices("OMNI_TIER_SHADOW_ASSIST_WILSON"),
    )
    omni_tier_assist_auto_wilson: float = Field(
        default=0.85,
        validation_alias=AliasChoices("OMNI_TIER_ASSIST_AUTO_WILSON"),
    )
    omni_tier_max_false_positive_rate: float = Field(
        default=0.10,
        validation_alias=AliasChoices("OMNI_TIER_MAX_FALSE_POSITIVE_RATE"),
    )
    omni_shadow_os_mode: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_SHADOW_OS_MODE"),
        description=(
            "Shadow OS governance mode: block SDK mutate execution and route to "
            "SUGGEST_OS_RUNBOOK / escalation path."
        ),
    )
    omni_executor_force_nsenter: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_EXECUTOR_FORCE_NSENTER"),
        description=(
            "Force executor command execution through nsenter host namespace wrapper "
            "(`nsenter -t 1 -m -u -i -n -p -- <command>`)."
        ),
    )
    omni_autonomous_rollout_on_cpu_incident: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_AUTONOMOUS_ROLLOUT_ON_CPU_INCIDENT"),
        description=(
            "When RAG hits on a workload CPU alert with namespace+deployment in evidence, "
            "also emit k8s_rollout_restart (not only when RAG text mentions restart). "
            "Disable for suggest-only/chatbot mode."
        ),
    )
    omni_autonomous_rollout_on_fault_incident: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_AUTONOMOUS_ROLLOUT_ON_FAULT_INCIDENT"),
        description=(
            "When evidence indicates workload fault (CreateContainer/CrashLoop/probe failure/ImagePull) "
            "with namespace+deployment labels, emit k8s_rollout_restart automatically."
        ),
    )
    autonomous_verify_max_rounds: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Max verify rounds (feedback loop) before escalate.",
    )
    omni_post_mutate_sdk_verify_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_POST_MUTATE_SDK_VERIFY_ENABLED"),
        description=(
            "After executor exit 0, re-run verify probes (SDK) before RAG VERIFIED; "
            "if FAILED, retry mutate within omni_sdk_verify_max_rounds / autonomous_execute_max_attempts."
        ),
    )
    omni_sdk_verify_max_rounds: int = Field(
        default=3,
        ge=1,
        le=20,
        validation_alias=AliasChoices("OMNI_SDK_VERIFY_MAX_ROUNDS"),
        description="Max post-mutate SDK verify cycles (drift still present → retry mutate).",
    )
    omni_sdk_verify_initial_delay_sec: float = Field(
        default=0.5,
        ge=0.0,
        le=120.0,
        validation_alias=AliasChoices("OMNI_SDK_VERIFY_INITIAL_DELAY_SEC"),
        description="Sleep before re-probe after mutate (API convergence).",
    )
    omni_sdk_verify_optional_probes: str = Field(
        default="prom_pod_cpu_cores,prom_pod_memory_wss",
        validation_alias=AliasChoices("OMNI_SDK_VERIFY_OPTIONAL_PROBES"),
        description=(
            "Comma-separated probe IDs allowed to be INCONCLUSIVE without failing post-mutate verify "
            "(e.g. missing Prom series). FAILED still fails."
        ),
    )
    omni_experience_requires_sdk_verify: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_EXPERIENCE_REQUIRES_SDK_VERIFY"),
        description=(
            "When true, RAG action_experience upsert only after SDK-verified success path; "
            "legacy finalize (verify disabled or no probes) skips upsert unless set false."
        ),
    )
    omni_verify_delay_sec: float = Field(
        default=10.0,
        ge=0.0,
        le=600.0,
        validation_alias=AliasChoices("OMNI_VERIFY_DELAY_SEC"),
        description=(
            "Cooldown after executor exit 0 before post-mutate re-probe (Kubernetes rollout/restart convergence). "
            "Used when OMNI_POST_MUTATE_VERIFY_PLANNER_ENABLED is true."
        ),
    )
    omni_state_verify_max_attempts: int = Field(
        default=2,
        ge=1,
        le=20,
        validation_alias=AliasChoices("OMNI_STATE_VERIFY_MAX_ATTEMPTS", "MAX_VERIFY_ATTEMPTS"),
        description=(
            "Max post-mutate state-verification cycles per trace (exit 0 → re-probe → planner gate). "
            "Above this → ESCALATE_TO_HUMAN."
        ),
    )
    omni_post_mutate_verify_planner_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_POST_MUTATE_VERIFY_PLANNER_ENABLED"),
        description=(
            "When true, executor exit_code=0 does not close incident until the LLM planner confirms "
            "recovery from fresh evidence; terminal close still requires Deployment state-machine healthy."
        ),
    )
    omni_post_mutate_state_verify_max_steps: int = Field(
        default=6,
        ge=1,
        le=24,
        validation_alias=AliasChoices("OMNI_POST_MUTATE_STATE_VERIFY_MAX_STEPS"),
        description="Max planner steps in the post-mutate state verification session.",
    )
    omni_post_verify_deployment_state_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_POST_VERIFY_DEPLOYMENT_STATE_ENABLED"),
        description=(
            "After SDK probes pass, re-check Deployment readiness (ready vs desired replicas) "
            "using workload identity — not pod names."
        ),
    )
    omni_telegram_suppress_when_deployment_healthy: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_TELEGRAM_SUPPRESS_WHEN_DEPLOYMENT_HEALTHY"),
        description=(
            "When SDK/planner would Telegram-escalate (e.g. SDK_VERIFY_NO_AGENTIC_PLAN, "
            "MAX_MUTATE_ATTEMPTS) but the alert Deployment rollout is healthy, close as STATE_MACHINE_VERIFIED "
            "instead (pod rotation / inconclusive PromQL are common false negatives)."
        ),
    )
    omni_post_verify_state_llm_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_POST_VERIFY_STATE_LLM_ENABLED"),
        description=(
            "If Deployment rollout still unhealthy after SDK probes OK, run local LLM CoT/ReAct "
            "with namespace+deployment context (pod names redacted): RETRY_ROLLOUT or fail closed."
        ),
    )
    omni_post_verify_react_max_steps: int = Field(
        default=6,
        ge=1,
        le=24,
        validation_alias=AliasChoices("OMNI_POST_VERIFY_REACT_MAX_STEPS"),
        description="Post-verify ReAct+CoT loop: max LLM rounds (read-only + mutate).",
    )
    omni_post_verify_react_readonly_max: int = Field(
        default=4,
        ge=0,
        le=16,
        validation_alias=AliasChoices("OMNI_POST_VERIFY_REACT_READONLY_MAX"),
        description="Max read-only discovery tool executions inside post-verify ReAct loop.",
    )
    omni_oom_deterministic_remediate_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_OOM_DETERMINISTIC_REMEDIATE_ENABLED"),
        description=(
            "Lab: emit k8s_patch_resource for OmniOomKilledPodNoRecovery when namespace+deployment "
            "labels exist (no probe extracted_fact required)."
        ),
    )
    autonomous_execute_max_attempts: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Max attempt_count (mutate + retries); above → ESCALATE_TO_HUMAN.",
    )
    omni_feedback_full_agentic_planner_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_FEEDBACK_FULL_AGENTIC_PLANNER_ENABLED"),
        description=(
            "After omni-action-feedback (SDK verify fail or mutate fail), run the same "
            "_emit_agentic_mutate_if_any pipeline on a fresh probe batch (same trace_id) before "
            "deterministic replan / one-shot LLM replan. Lab-only recommended."
        ),
    )
    omni_llm_trace_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_LLM_TRACE_ENABLED"),
        description=(
            "INFO logs: raw LLM body + parse_ok / reject_reason for agentic planner, RAG-miss SDK-only, "
            "post-verify ReAct. Use for debugging Ollama output vs tool JSON contract."
        ),
    )
    executor_action_rate_limit_burst: int = Field(
        default=6,
        ge=1,
        le=200,
        description="Max EXECUTE_MUTATE per action fingerprint within window before skip/escalate.",
    )
    executor_action_rate_limit_window_sec: int = Field(
        default=60,
        ge=1,
        le=3600,
        description="Rate limit window in seconds for executor action fingerprint control.",
    )
    autonomous_agentic_max_steps: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Max LLM agentic steps when RAG does not hit.",
    )
    omni_llm_first_autonomy_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_LLM_FIRST_AUTONOMY_ENABLED"),
        description=(
            "Enable LLM-first autonomy: discovery->precondition gate->mutate, "
            "without deterministic mutate shortcuts in the mainline."
        ),
    )
    omni_unrestricted_tool_execution: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_UNRESTRICTED_TOOL_EXECUTION"),
        description=(
            "When true, EXECUTE_MUTATE bypasses mutate-only allowlist at executor boundary "
            "(still subject to mutate_governance: prod kubectl_cluster / high-risk gates). "
            "Default false — lab may set true for broad tool experiments."
        ),
    )
    omni_kubectl_cluster_mutate_allowed: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_KUBECTL_CLUSTER_MUTATE_ALLOWED"),
        description=(
            "Prod only: allow kubectl_cluster via EXECUTE_MUTATE when unrestricted or allowlisted. "
            "Default false — prefer SDK mutating tools."
        ),
    )
    omni_high_risk_mutate_allowed: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_HIGH_RISK_MUTATE_ALLOWED"),
        description=(
            "Prod only: allow high-risk mutating tools (delete pod, patch secret, apply RBAC). "
            "Default false — pair with tight RBAC and namespace allowlist."
        ),
    )
    omni_planner_precondition_gate_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_PLANNER_PRECONDITION_GATE_ENABLED"),
        description=(
            "Validate mutate tool preconditions (from ToolRegistry metadata + trace evidence) "
            "before EXECUTE_MUTATE emission. Missing prerequisites trigger planner re-ask."
        ),
    )
    omni_legacy_deterministic_fallback: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_LEGACY_DETERMINISTIC_FALLBACK"),
        description=(
            "When true: deterministic mutate shortcuts (rollout/configmap/secret patterns) when planner output is empty. "
            "Default false: planner + SDK-verify path only (no legacy shortcuts)."
        ),
    )
    omni_diagnostic_react_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_DIAGNOSTIC_REACT_ENABLED"),
        description="Interleave read-only k8s tools in agentic planner before mutate (ReAct).",
    )
    omni_diagnostic_react_readonly_max: int = Field(
        default=3,
        ge=0,
        le=12,
        validation_alias=AliasChoices("OMNI_DIAGNOSTIC_REACT_READONLY_MAX"),
        description="Max read-only tool rounds per agentic plan when react enabled.",
    )
    omni_discovery_mandatory: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_DISCOVERY_MANDATORY"),
        description=(
            "When true (recommended in prod strict posture): block EXECUTE_MUTATE unless discovery-equivalent "
            "evidence exists — planner discovery_steps, successful trace-memory readonly executions, or batch "
            "probes indicating K8s/Prometheus/Loki/redis/service topology reads (see diagnostic_policy). "
            "Default false so lab/unit tests stay unchanged unless env set."
        ),
    )
    omni_planner_llm_sole_evaluator: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_PLANNER_LLM_SOLE_EVALUATOR"),
        description=(
            "When true: LLM alone judges resolution vs InitialSymptom/HISTORY; disable regex-based "
            "planner hints (_general_credential_failure_hint, _broken_spec_first_round_instruction)."
        ),
    )
    omni_trace_memory_tool_output_max_chars: int = Field(
        default=4000,
        ge=400,
        le=16000,
        validation_alias=AliasChoices("OMNI_TRACE_MEMORY_TOOL_OUTPUT_MAX_CHARS"),
        description="Max chars stored per ActionRecord.result_summary in Redis trace memory (planner read-only output).",
    )
    lab_chaos_credential_autofix_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_LAB_CHAOS_CREDENTIAL_AUTOFIX_ENABLED"),
        description=(
            "Lab: chaos DB password auth failure → deterministic k8s_patch_secret using "
            "chaos_pg_app_password. Disable in prod; pair with OMNI_CHAOS_PG_APP_PASSWORD in ConfigMap."
        ),
    )
    chaos_pg_app_password: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("OMNI_CHAOS_PG_APP_PASSWORD"),
        description="Lab chaos-pg: plain APP_PASSWORD value for autofix (omit in prod).",
    )
    chaos_pg_secret_name: str = Field(
        default="chaos-pg-secret",
        validation_alias=AliasChoices("OMNI_CHAOS_PG_SECRET_NAME"),
        description="Secret name for chaos credential lab autofix.",
    )
    chaos_pg_password_key: str = Field(
        default="APP_PASSWORD",
        validation_alias=AliasChoices("OMNI_CHAOS_PG_PASSWORD_KEY"),
        description="Secret data key patched by chaos credential lab autofix.",
    )
    omni_blind_lane_llm_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_BLIND_LANE_LLM_ENABLED"),
        description="When matrix row missing, ask local LLM for proof_lane (resource|state|app_log).",
    )
    autonomous_sigma_observation_window: int = Field(
        default=1,
        ge=1,
        le=20,
        validation_alias=AliasChoices("OMNI_AUTONOMOUS_SIGMA_OBSERVATION_WINDOW"),
        description="Consecutive proof+sigma passes required before EXECUTE_MUTATE.",
    )
    omni_proof_lane_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_PROOF_LANE_ENABLED"),
        description="When true: proof-of-fault uses resource/state/app_log lanes (matrix + heuristics). When false: legacy sigma + optional log bypass.",
    )
    omni_sigma_log_bypass_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_SIGMA_LOG_BYPASS_ENABLED"),
        description="When true: sigma gate may be satisfied by Loki sustained 5xx evidence (API/Web workload).",
    )
    omni_loki_base_url: str = Field(
        default="http://loki.monitor.svc.cluster.local:3100",
        validation_alias=AliasChoices("OMNI_LOKI_BASE_URL"),
        description="Base URL for Loki query_range (log surge probe).",
    )
    omni_log_surge_window_sec: int = Field(
        default=300,
        ge=60,
        le=3600,
        validation_alias=AliasChoices("OMNI_LOG_SURGE_WINDOW_SEC"),
        description="Lookback window for Loki log surge probe.",
    )
    omni_log_surge_min_lines: int = Field(
        default=5,
        ge=2,
        le=500,
        validation_alias=AliasChoices("OMNI_LOG_SURGE_MIN_LINES"),
        description="Minimum parsed lines (access status or JSON rows) before ratio applies.",
    )
    omni_log_surge_min_ratio: float = Field(
        default=0.01,
        ge=0.001,
        le=1.0,
        validation_alias=AliasChoices("OMNI_LOG_SURGE_MIN_RATIO"),
        description="Minimum fraction of parsed log lines that are 5xx (500/503/504) or JSON error (e.g. 0.01 = 1%).",
    )
    omni_log_surge_line_limit: int = Field(
        default=500,
        ge=50,
        le=5000,
        validation_alias=AliasChoices("OMNI_LOG_SURGE_LINE_LIMIT"),
        description="Loki query_range line limit for surge probe.",
    )
    omni_log_surge_http_timeout_sec: float = Field(
        default=25.0,
        ge=3.0,
        le=120.0,
        validation_alias=AliasChoices("OMNI_LOG_SURGE_HTTP_TIMEOUT_SEC"),
        description="HTTP timeout for Loki request from analyst.",
    )
    trace_correlation_ping_enabled: bool = Field(
        default=True,
        description="After analyst handles diagnostic evidence, emit omni-actions SUGGEST_REMEDIATION with same trace_id (unified tracing).",
    )

    @field_validator(
        "kafka_topic_alerts",
        "kafka_topic_dlq",
        "kafka_topic_proactive_incidents",
        "kafka_topic_audit_sandbox",
        "kafka_topic_audit_proactive",
        "kafka_topic_audit_agent",
        "kafka_topic_diagnostic_evidence",
        "kafka_topic_discovery_evidence",
        "kafka_topic_tool_audit",
        "kafka_topic_actions",
        "kafka_topic_action_feedback",
        "kafka_topic_audit_chain",
        mode="after",
    )
    @classmethod
    def _sanitize_kafka_topic_names(cls, v: str, info: ValidationInfo) -> str:
        """Legacy Redis stream keys (e.g. ``events:inbound``) are invalid Kafka topic names — fall back."""
        defaults: dict[str, str] = {
            "kafka_topic_alerts": "omni-alerts",
            "kafka_topic_dlq": "omni-dlq",
            "kafka_topic_proactive_incidents": "omni-proactive-incidents",
            "kafka_topic_audit_sandbox": "omni-audit-sandbox",
            "kafka_topic_audit_proactive": "omni-audit-proactive",
            "kafka_topic_audit_agent": "omni-audit-agent",
            "kafka_topic_diagnostic_evidence": "omni-diagnostic-evidence",
            "kafka_topic_discovery_evidence": "omni-discovery-evidence",
            "kafka_topic_tool_audit": "omni-tool-audit",
            "kafka_topic_actions": "omni-actions",
            "kafka_topic_action_feedback": "omni-action-feedback",
            "kafka_topic_audit_chain": "omni-audit-chain",
        }
        fb = defaults.get(info.field_name or "", "omni-alerts")
        if not isinstance(v, str) or not v.strip():
            return fb
        s = v.strip()
        if not re.match(r"^[a-zA-Z0-9._-]+$", s):
            return fb
        return s

    consumer_group: str = Field(default="omni-worker-alerts")
    consumer_name: str = Field(default="omni-worker-1")
    consumer_group_analyst: str = Field(
        default="omni-analyst-evidence",
        description="Kafka group for svc-analyst — consumes omni-diagnostic-evidence only.",
    )
    consumer_group_analyst_feedback: str = Field(
        default="omni-analyst-action-feedback",
        description="Kafka group for analyst — consumes omni-action-feedback.",
    )
    consumer_name_analyst: str = Field(default="omni-analyst-1")
    consumer_group_chain: str = Field(
        default="omni-analyst-siem-chains",
        description="Kafka group for analyst — consumes omni-siem-chains correlation chains.",
    )
    siem_chain_consumer_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_SIEM_CHAIN_CONSUMER_ENABLED"),
        description="Enable the omni-siem-chains correlation-chain consumer loop (analyst role).",
    )
    siem_correlation_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_SIEM_CORRELATION_ENABLED"),
        description=(
            "Enable the in-process SIEM graph-correlation engine "
            "(services.siem_correlation — Python port of brain-go): consumes "
            "omni-siem-raw, produces omni-siem-incidents + omni-siem-chains. "
            "Default off; the deployment manifest turns it on explicitly."
        ),
    )
    consumer_group_executor: str = Field(
        default="omni-executor-actions",
        description="Kafka group for svc-executor — consumes omni-actions only.",
    )
    consumer_name_executor: str = Field(default="omni-executor-1")
    consumer_group_onboarding: str = Field(
        default="omni-onboarding-discovery",
        description="Kafka group for onboarding role — consumes omni-discovery-evidence only.",
    )
    consumer_name_onboarding: str = Field(default="omni-onboarding-1")
    consumer_group_knowledge: str = Field(
        default="omni-knowledge-worker",
        description="Kafka group for knowledge worker — consumes omni-knowledge-evidence (METRIC_SAMPLE/LOG_SAMPLE/DISCOVERY/CHANGE_DETECTED).",
    )
    consumer_name_knowledge: str = Field(default="omni-knowledge-1")
    env_mode: Literal["prod", "dev"] = Field(
        default="prod",
        validation_alias=AliasChoices("OMNI_ENV_MODE"),
        description="Environment governance mode: prod=strict fail-closed, dev=high-action by role.",
    )
    worker_role: Literal["full", "prober", "analyst", "core", "executor", "onboarding"] = Field(
        default="full",
        description=(
            "Master Plan V3 process split: prober=omni-alerts+diagnostic; analyst=evidence only; "
            "core=periodic/proactive without Kafka ingress; executor=omni-actions mutations; "
            "onboarding=omni-discovery-evidence accumulate+mermaid+ask-loop; full=legacy single process."
        ),
    )

    @field_validator("env_mode", mode="before")
    @classmethod
    def _normalize_env_mode(cls, v: object) -> object:
        # CLAUDE.md / INVARIANTS use OMNI_ENV_MODE=lab; the governance model only
        # distinguishes prod (strict fail-closed) vs non-prod (high-action by role).
        # Map the lab alias to dev so OMNI_ENV_MODE=lab does not crash validation.
        if isinstance(v, str):
            s = v.strip().lower()
            if s == "lab":
                return "dev"
            if s in ("prod", "dev"):
                return s
        return v

    @field_validator("worker_role", mode="before")
    @classmethod
    def _normalize_worker_role(cls, v: object) -> object:
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("full", "prober", "analyst", "core", "executor", "onboarding"):
                return s
        return v

    block_ms: int = Field(default=5000, ge=500)
    
    # Banking-Grade Resilience Limits
    cb_max_delayed_queue: int = Field(default=5000, ge=100, le=100_000, description="Ngưỡng 5000 kích hoạt Circuit Breaker chặn Ingress Gateway.")
    idempotency_ttl_sec: int = Field(default=120, ge=30, le=3600, description="TTL cho Atomic PENDING Lock của Action Guard.")
    gateway_rate_limit_tps: int = Field(default=1000, ge=10, le=10_000, description="Maximum TPS cho Gateway Ingress /webhook/prometheus.")

    llm_num_parallel: int = Field(
        default=2,
        ge=1,
        le=32,
        description="Parallel LLM slots (semaphore); env OMNI_LLM_NUM_PARALLEL.",
    )
    # OrbStack DNS: pods reach Ollama on macOS host via host.orb.internal (see omni-worker-configmap).
    vllm_base_url: str = Field(
        default="http://host.orb.internal:11434/v1",
        validation_alias=AliasChoices("OMNI_VLLM_BASE_URL", "OMNI_OLLAMA_BASE_URL"),
        description="OpenAI-compat chat base URL (/v1 optional). Alias OMNI_OLLAMA_BASE_URL for legacy env.",
    )
    vllm_embed_url: str = Field(
        default="http://host.orb.internal:11434/v1",
        validation_alias=AliasChoices("OMNI_VLLM_EMBED_URL", "OMNI_OLLAMA_EMBED_URL"),
        description="Embeddings endpoint base URL; alias OMNI_OLLAMA_EMBED_URL.",
    )
    # Tier-1 DEFAULT_WORKER — SDK tool JSON, status, chart
    chat_model: str = Field(default="qwen2.5-coder:7b")
    model_reasoning_engine: str = Field(default="qwen2.5-coder:7b")
    model_heavy_lifter: str = Field(default="qwen2.5-coder:7b")
    model_helper: str = Field(default="qwen2.5-coder:7b")
    embed_model: str = Field(default="nomic-embed-text")
    #: Fallback embed model; empty = chỉ retry truncate khi error.
    embed_model_fallback: str = Field(
        default="",
        description="Optional fallback embed model; empty = no fallback.",
    )
    rag_embed_max_tokens: int = Field(default=512, ge=64, le=2048)
    llm_lease_ttl_sec: int = Field(default=120, ge=10)

    # --- pgvector: một bảng rag_documents, phân vùng theo collection_name (API gọi là collection_id) ---
    pgvector_collection_k8s_expert: str = Field(
        default="k8s_expert",
        min_length=1,
        max_length=64,
        description="Partition LIST trùng tên collection; RAG tri thức K8s/ingest + analyst similarity_search.",
    )
    k8s_official_docs_base_url: str = Field(
        default="https://kubernetes.io",
        description="Host gốc crawler docs (OMNI_K8S_OFFICIAL_DOCS_BASE_URL).",
    )
    k8s_official_docs_seed_urls: str = Field(
        default="https://kubernetes.io/docs/home/,https://kubernetes.io/docs/concepts/,https://kubernetes.io/docs/tasks/",
        description="Danh sách URL bắt đầu crawl, phân tách bởi dấu phẩy.",
    )
    k8s_official_crawl_max_pages: int = Field(default=48, ge=1, le=5000)
    k8s_official_crawl_max_depth: int = Field(default=2, ge=0, le=6)
    k8s_official_chunk_chars: int = Field(default=1400, ge=400, le=12000)
    k8s_official_chunk_overlap: int = Field(default=140, ge=0, le=2000)
    k8s_official_crawl_timeout_sec: float = Field(default=25.0, ge=5.0, le=120.0)
    k8s_official_request_delay_sec: float = Field(default=0.4, ge=0.0, le=10.0)
    k8s_official_metadata_version: str = Field(
        default="1.30",
        min_length=1,
        max_length=32,
        description="Ghi vào payload.metadata.version cho chunk kubernetes.io.",
    )
    k8s_official_user_agent: str = Field(
        default="OmniLabK8sOfficialIngest/1.0 (+https://kubernetes.io)",
        min_length=8,
        max_length=256,
    )
    k8s_official_docs_path_prefix: str = Field(
        default="/docs/",
        min_length=1,
        max_length=128,
        description="Chỉ enqueue link cùng host và path bắt đầu bằng prefix này.",
    )
    k8s_official_sitemap_url: str = Field(
        default="https://kubernetes.io/sitemap.xml",
        description="Sitemap XML để seed thêm URL (để trống = không fetch sitemap). OMNI_K8S_OFFICIAL_SITEMAP_URL",
    )
    k8s_official_sitemap_max_urls: int = Field(
        default=120,
        ge=0,
        le=2000,
        description="Tối đa URL lấy từ sitemap mỗi lần ingest (0 = không lấy từ sitemap).",
    )

    # Unified RagGate (k8s_expert) — trước LLM / fast path.
    rag_gate_enabled: bool = Field(default=True, description="Bật cổng RAG trước khi preflight/LLM.")
    rag_gate_score_threshold: float = Field(default=0.42, ge=0.0, le=1.0)
    rag_gate_limit: int = Field(default=4, ge=1, le=24)
    rag_gate_query_max_chars: int = Field(default=8000, ge=500, le=24_000)
    rag_gate_chunk_max_chars: int = Field(default=1200, ge=200, le=4000)
    omni_summary_max_words: int = Field(
        default=100,
        ge=10,
        le=500,
        description="Trần cứng tóm tắt; dùng cùng concise_reply (min của hai).",
    )
    omni_concise_reply_max_words: int = Field(
        default=30,
        ge=10,
        le=200,
        description="Phản hồi analyst/RAG/Telegram proactive — tối đa từ (local LLM; mặc định ~30).",
    )
    omni_advisory_num_predict: int = Field(
        default=1024,
        ge=256,
        le=4096,
        description=(
            "Ollama num_predict for Advisory JSON (omni-advisory-analyst). Lower=faster; too low may truncate JSON. "
            "OMNI_ADVISORY_NUM_PREDICT"
        ),
    )
    infra_enrich_max_total_chars: int = Field(
        default=6000,
        ge=2000,
        le=50_000,
        description="Trần tổng ký tự sau enrich_working_text_with_infra (mọi source, kể cả prometheus).",
    )

    rag_fast_path_score: float = Field(default=0.9, ge=0.0, le=1.0)

    # Phase ZERO — SRE truth / hybrid / hot cache (see plan: RAG infra upgrade)
    rag_truth_law_enforced: bool = Field(
        default=True,
        description="Khi không RAG hit: không gọi LLM chẩn đoán tự do — trả token manual.",
    )
    rag_manual_unknown_token: str = Field(
        default="I_DO_NOT_KNOW_PROCEED_TO_MANUAL",
        description="Chuỗi cố định khi không đủ căn cứ RAG.",
    )
    rag_post_filter_metadata_enabled: bool = Field(
        default=True,
        description="Incident-like query: bỏ chunk metadata type=reference thuần (Phase A).",
    )
    rag_hybrid_search_enabled: bool = Field(
        default=False,
        description="Postgres dense + full-text RRF trong pgvector_store.",
    )
    rag_hybrid_vector_weight: float = Field(default=0.65, ge=0.0, le=1.0)
    rag_hot_cache_enabled: bool = Field(default=False, description="Redis JSON cache trước similarity_search.")
    rag_hot_cache_ttl_sec: int = Field(default=3600, ge=60, le=86400)
    rag_tier_uncertain_gate_enabled: bool = Field(
        default=False,
        description="Bật mới áp ngưỡng knowledge_uncertain (tránh đổi hành vi mặc định).",
    )
    rag_tier_knowledge_uncertain_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    rag_evidence_contradiction_check_enabled: bool = Field(
        default=True,
        description="So khớp output LLM vs SDK facts — chặn remediation mâu thuẫn.",
    )
    pre_action_state_revalidate_enabled: bool = Field(
        default=True,
        description="Trước rollout restart: so generation snapshot vs live Deployment.",
    )
    pgvector_collection_k8s_troubleshoot: str = Field(
        default="",
        description="Rỗng = dùng pgvector_collection_k8s_expert; Phase A2 incident routing.",
    )
    diag_evidence_llm_model: str = Field(
        default="",
        description="Rỗng = model_reasoning_engine; có thể đặt model_helper cho tier nhẹ.",
    )
    rag_rerank_enabled: bool = Field(default=False, description="Reserved: cross-encoder rerank sau vector.")

    # Telegram session_state:{chat_id} — TTL Redis (giây)
    session_ttl_sec: int = Field(default=86400, ge=120)
    # turn_count > threshold → compressor (1.5B) tóm tắt trước khi gọi 7B
    compress_turn_threshold: int = Field(default=5, ge=1)
    # Slow-path: tối đa vòng chat(JSON)+thực thi tool; hết vòng → autopsy + Postgres exhausted.
    slow_path_max_tool_attempts: int = Field(default=5, ge=1, le=16)
    # JSON repair (helper model) tách khỏi số vòng tool ở trên — mỗi vòng có tối đa json_repair_max lần gọi helper.
    json_repair_max: int = Field(default=3, ge=1, le=10)
    # Cùng error_signature liên tiếp đủ streak → thoát sớm (tiết kiệm token M4).
    slow_path_stale_signature_streak: int = Field(default=3, ge=2, le=8)

    telegram_enabled: bool = Field(default=True)
    telegram_polling_enabled: bool = Field(
        default=True,
        description=(
            "Khi False: không chạy telegram_loop (getUpdates) trên prober/full — "
            "để harness E2E gọi Bot API getUpdates từ ngoài không bị tranh offset; "
            "analyst/core vẫn dùng TelegramClient.send_message cho advisory."
        ),
    )

    # Mặc định khi tool k8s_list_pods không truyền args.namespace — khớp RBAC Role namespace.
    k8s_default_namespace: str = Field(default="multi-agent", min_length=1)

    prometheus_url: str = Field(
        default="prometheus.monitor.svc.cluster.local:9090",
        validation_alias=AliasChoices("OMNI_PROMETHEUS_URL", "OMNI_VICTORIA_METRICS_URL"),
        description=(
            "Prometheus HTTP (PromQL /api/v1/query, /api/v1/query_range). "
            "Legacy env: OMNI_VICTORIA_METRICS_URL (VictoriaMetrics đã thay bằng Prometheus)."
        ),
    )
    vmagent_url: str = Field(
        default="prometheus.monitor.svc.cluster.local:9090",
        validation_alias=AliasChoices("OMNI_VMAGENT_URL", "OMNI_PROMETHEUS_TARGETS_URL"),
        description="Prometheus — /api/v1/targets (scrape health). Legacy: OMNI_VMAGENT_URL (vmagent).",
    )

    @field_validator("prometheus_url", "vmagent_url", mode="before")
    @classmethod
    def _normalize_prometheus_http_scheme(cls, v: object) -> object:
        return _coerce_http_base_url(v)
    deep_scout_interval_sec: int = Field(default=900, ge=120, le=86400)

    # Autonomous Prometheus → Prophet (omni-worker asyncio task; cần telegram_admin_chat_id)
    autonomous_forecast_enabled: bool = Field(
        default=False,
        description="Bật vòng lặp định kỳ: PromQL → Prophet/fallback → cảnh báo Telegram nếu vượt ngưỡng.",
    )
    autonomous_forecast_interval_sec: int = Field(default=300, ge=60, le=86400)
    autonomous_forecast_promql: str = Field(
        default='avg(rate(node_cpu_seconds_total{mode!="idle"}[5m]))',
        description="PromQL một series; chỉnh theo cluster (đơn vị phải khớp threshold).",
    )
    autonomous_forecast_threshold: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="So với yhat_max/yhat_last (tùy metric — chỉnh PromQL + threshold cùng nhau).",
    )
    autonomous_forecast_duration: str = Field(default="24h", description="Cửa sổ lịch sử (vd 24h).")
    autonomous_forecast_periods: int = Field(default=12, ge=2, le=500)
    autonomous_forecast_alert_cooldown_sec: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="Redis dedupe — không spam Telegram cùng một cảnh báo.",
    )
    # Prometheus instant → Redis baseline (slow-path system prompt; không dump TSDB)
    baseline_snapshot_enabled: bool = Field(
        default=False,
        description="baseline_sync_loop: nhiều PromQL instant → Redis omni:baseline:snapshot.",
    )
    baseline_snapshot_interval_sec: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Chu kỳ sync (mặc định 300s).",
    )
    baseline_promql: str = Field(
        default="",
        validation_alias=AliasChoices("OMNI_BASELINE_PROMQL"),
        description="Tuỳ chọn: thêm dòng name|instant_promql → merge vào key q trong manifest.",
    )
    baseline_manifest_max_chars: int = Field(
        default=1400,
        ge=400,
        le=4096,
        description="Giới hạn độ dài JSON System Health Manifest (Redis).",
    )
    baseline_smart_trim_window_min: int = Field(
        default=10,
        ge=1,
        le=60,
        validation_alias=AliasChoices("OMNI_BASELINE_SMART_TRIM_WINDOW_MIN"),
        description="Smart trim: events older than N minutes (relative to manifest t) are trimmed first.",
    )
    baseline_smart_trim_keywords: str = Field(
        default="OOM|panic|deadlock|segfault|Timeout|refused|Exception|Failed",
        validation_alias=AliasChoices("OMNI_BASELINE_SMART_TRIM_KEYWORDS"),
        description="Pipe-separated regex keywords that protect events/logs from smart trim. Covers K8s, bare-metal, VMs, DBs, middleware.",
    )
    baseline_cpu_drift_threshold: float = Field(
        default=0.15,
        ge=0.01,
        le=1.0,
        description="Legacy drift cpu: |new-old|/|old| > threshold → dr=true khi baseline_legacy_cpu_drift_for_dr.",
    )
    baseline_legacy_cpu_drift_for_dr: bool = Field(
        default=False,
        description="Nếu true: dr cũng bật theo drift % hai snapshot (song song 3-Sigma). Mặc định chỉ dùng |z|.",
    )
    baseline_dr_z_threshold: float = Field(
        default=3.0,
        ge=0.5,
        le=20.0,
        description="3-Sigma: dr=true nếu abs(z_cpu) hoặc abs(z_mem) vượt ngưỡng (instant từ recording rules).",
    )
    baseline_promql_z_cpu: str = Field(
        default="omni:node_cpu:z",
        description="Instant PromQL scalar cho z-score CPU (recording rule omni:node_cpu:z).",
    )
    baseline_promql_z_mem: str = Field(
        default="omni:mem:z",
        description="Instant PromQL scalar cho z-score mem (recording rule omni:mem:z).",
    )
    baseline_promql_z_disk: str = Field(
        default="omni:node_disk:z",
        description="Instant PromQL scalar cho z-score disk (recording omni:node_disk:z).",
    )
    baseline_promql_z_net: str = Field(
        default="",
        description="Rỗng = không query; z_net=0 khi tính CHS nếu có weight net.",
    )
    baseline_promql_seasonal_cpu: str = Field(
        default="omni:health:cpu_seasonal_drift_z",
        description="Recording WoW seasonal drift Z CPU; fallback trong rule Prometheus.",
    )
    omni_readonly_tool_cache_ttl_sec: int = Field(default=300, ge=10, le=3600)
    chs_weights: str = Field(
        default="",
        description='JSON trọng số CHS (OMNI_CHS_WEIGHTS), ví dụ {"cpu":0.25,"mem":0.25,"disk":0.25,"net":0.25}. Rỗng = không ghi chs.',
    )
    chs_threshold: float = Field(
        default=10.0,
        ge=0.0,
        le=1_000_000.0,
        validation_alias=AliasChoices("OMNI_CHS_THRESHOLD"),
        description="wide_incident khi chs > ngưỡng.",
    )
    golden_latency_promql: str = Field(
        default="",
        validation_alias=AliasChoices("OMNI_GOLDEN_LATENCY_PROMQL"),
        description="Instant PromQL scalar cho latency P99 (thường giây → chuyển ms trong worker).",
    )
    latency_threshold_ms: float | None = Field(
        default=None,
        ge=0.0,
        le=1e9,
        validation_alias=AliasChoices("OMNI_LATENCY_THRESHOLD_MS"),
        description="Nếu latency đo được và < ngưỡng (ms) → remediation_silent=true. None = tắt nhánh này.",
    )

    @field_validator("latency_threshold_ms", mode="before")
    @classmethod
    def _coerce_latency_threshold_ms(cls, v: object) -> object:
        if v is None or v == "" or (isinstance(v, str) and v.strip().lower() in ("none", "null")):
            return None
        return v

    autonomous_decider_enabled: bool = Field(
        default=False,
        description="Vòng autonomous_decider_loop: LLM + allowlist khi dr hoặc evt.",
    )
    autonomous_decider_interval_sec: int = Field(default=300, ge=30, le=3600)
    autonomous_decider_model: str = Field(
        default="",
        description="Rỗng = dùng model_reasoning_engine.",
    )
    autonomous_fix_cooldown_sec: int = Field(default=600, ge=60, le=86400)
    meta_self_alert_cooldown_sec: int = Field(
        default=1800, ge=60, le=86400,
        description="Dedup window cho self-monitoring/KPI alert (OmniAdvisoryAcceptanceRateLow,...): "
        "1 escalation mỗi cooldown, không lặp lại RAG/LLM/DLQ mỗi tick re-fire.",
    )
    autonomous_react_enabled: bool = Field(
        default=True,
        description="True: multi-turn ReAct (Thought/Observation); False: legacy one-shot decider.",
    )
    react_max_turns: int = Field(default=4, ge=1, le=24, description="Max ReAct turns trước [REACT_ABORTED].")
    react_observation_max_chars: int = Field(
        default=1200,
        ge=200,
        le=4000,
        description="Cắt + sanitize Observation trước khi nạp lại LLM.",
    )
    react_state_redis_ttl_sec: int = Field(
        default=1200,
        ge=60,
        le=86400,
        description="TTL key omni:autonomous:react_state:*.",
    )
    approval_request_ttl_sec: int = Field(
        default=600,
        ge=60,
        le=86400,
        description="TTL key omni:approval:* (human-in-the-loop).",
    )
    tool_output_max_chars: int = Field(
        default=1500,
        ge=400,
        le=8000,
        description="Cắt + sanitize mọi tool return (registry + legacy) trước khi đưa user/LLM.",
    )
    autonomous_safe_tools: str = Field(
        default="k8s_rollout_restart,redis_health,redis_expert_check,sandbox_cleanup,k8s_list_nodes,k8s_node_conditions,k8s_list_services,k8s_list_ingress",
        description="CSV tool names cho Autonomous Decider.",
    )
    autonomous_allowed_namespaces: str = Field(
        default="multi-agent",
        description="CSV — k8s_rollout_restart chỉ khi namespace thuộc danh sách.",
    )
    omni_probe_driven_mutate_tools: str = Field(
        default="k8s_apply_rbac_least_privilege,k8s_patch_configmap",
        validation_alias=AliasChoices("OMNI_PROBE_DRIVEN_MUTATE_TOOLS"),
        description=(
            "CSV — probe FAILED + structured_hint may skip LLM for these tools when args are complete. "
            "Intersected with EXECUTE_MUTATE allowlist."
        ),
    )
    baseline_warning_events_max: int = Field(default=5, ge=1, le=20)
    baseline_warning_events_fetch_limit: int = Field(default=400, ge=50, le=2000)
    baseline_k8s_events_timeout_sec: float = Field(default=20.0, ge=3.0, le=120.0)
    baseline_snapshot_redis_ttl_sec: int = Field(
        default=600,
        ge=120,
        le=86400,
        description="TTL key omni:baseline:snapshot.",
    )
    baseline_system_prompt_max_chars: int = Field(
        default=1600,
        ge=400,
        le=4096,
        description="Truncate header + manifest JSON trong slow-path.",
    )
    baseline_snapshot_max_chars: int = Field(
        default=512,
        ge=64,
        le=4096,
        description="Hint legacy / debug — fetch_baseline_snapshot_hint.",
    )
    deep_scout_embed_concurrency: int = Field(default=5, ge=1, le=32)
    deep_scout_configmap_namespaces: str = Field(
        default="multi-agent,monitor",
        description="CSV namespaces — chỉ quét ConfigMap ở đây (giảm rủi ro).",
    )
    telegram_admin_chat_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("OMNI_TELEGRAM_ADMIN_CHAT_ID", "TELEGRAM_CHAT_ID"),
        description="Báo cáo Deep Scout; None = không gửi.",
    )
    omni_operator_digest_locale: Literal["en", "vi", "both"] = Field(
        default="vi",
        validation_alias=AliasChoices("OMNI_OPERATOR_DIGEST_LOCALE"),
        description="Contrast digest Telegram/inbound: en | vi | both (deterministic copy). "
        "Default vi — operators are Vietnamese; both/en available via env for debugging.",
    )
    # Postgres removed — Omni RAG is on Redis Stack (HNSW + semantic cache).
    # Keep field as deprecated placeholder for any legacy caller that references it;
    # actual storage is `redis_url`. Remove when callers are fully purged.
    postgres_dsn: str = Field(
        default="",
        description="DEPRECATED — Omni RAG migrated to Redis Stack. Do not set.",
    )

    # PostgreSQL — Admin config source-of-truth (omni_admin schema, MASTER_PLAN §6.5).
    # Shared cluster với FinGuard. Rỗng = Admin config store disabled (lab fallback env).
    admin_pg_dsn: str = Field(
        default="",
        validation_alias=AliasChoices("OMNI_ADMIN_PG_DSN"),
        description="asyncpg DSN cho schema omni_admin (config-of-record). Rỗng = disabled.",
    )
    admin_pg_pool_min: int = Field(default=1, ge=1, le=16)
    admin_pg_pool_max: int = Field(default=8, ge=1, le=64)
    # CRAT outbox drainer (Transactional Outbox — MASTER_PLAN §7). Chạy ở role analyst/full.
    crat_outbox_drainer_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_CRAT_OUTBOX_DRAINER_ENABLED"),
        description="Bật loop drain crat_outbox → write_audit_block (at-least-once).",
    )
    crat_outbox_poll_interval_sec: float = Field(default=5.0, ge=0.5, le=120.0)
    crat_outbox_batch_size: int = Field(default=32, ge=1, le=256)
    crat_outbox_max_attempts: int = Field(default=10, ge=1, le=100)

    # Scope advocacy — Omni tự đánh giá năng lực rồi tự nộp đơn xin thêm quyền
    # (plans/case-ledger-design-2026-07-30.md, mục "Xin quyền"). Chỉ role `full`.
    #
    # Default TẮT, theo đúng nguyên tắc fail-closed của dự án (giống
    # OMNI_AUTO_EXECUTE_ENABLED): đây là hành vi Omni CHỦ ĐỘNG ghi vào bảng quyền
    # của tenant khách và đẩy đơn tới bàn của admin khách. Đơn không tự cấp quyền
    # — người vẫn phải duyệt — nhưng việc một hệ thống tự động bắt đầu gõ cửa
    # admin phải là một quyết định có ý thức của người vận hành, không phải hệ quả
    # phụ của một lần deploy.
    scope_advocacy_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_SCOPE_ADVOCACY_ENABLED"),
        description="Bật loop Omni tự nộp đơn xin mở rộng quyền theo pattern_key.",
    )
    # Chu kỳ tính bằng GIỜ, default 24h. Bằng chứng năng lực tích luỹ theo tuần/
    # tháng (cần ca mới + verdict từ người/thế giới), nên chạy dày hơn một ngày
    # không sinh thêm thông tin — chỉ đốt kết nối PG và spam đơn vào cùng một
    # pattern. Cận dưới Wilson gần như không đổi trong vài giờ.
    scope_advocacy_interval_hours: float = Field(
        default=24.0,
        ge=1.0,
        le=720.0,
        validation_alias=AliasChoices("OMNI_SCOPE_ADVOCACY_INTERVAL_HOURS"),
        description="Chu kỳ chạy vòng tự xin quyền, đơn vị giờ (default 24h).",
    )

    # Deep Scout autonomous (1.5B synthesis + Redis/Postgres)
    autonomous_scout_max_pods: int = Field(default=40, ge=5, le=500)
    autonomous_scout_max_services: int = Field(default=80, ge=10, le=500)
    autonomous_synth_concurrency: int = Field(default=2, ge=1, le=8)
    autonomous_probe_enabled: bool = Field(
        default=False,
        description="Bật probe shell qua OpenSandbox sau synthesis (cần opensandbox_enabled).",
    )
    learned_map_ttl_sec: int = Field(default=3600, ge=300, le=86400)

    # OpenSandbox execution plane (HTTP API — không subprocess trên omni-worker)
    opensandbox_enabled: bool = Field(default=False)
    opensandbox_base_url: str = Field(
        default="http://opensandbox-shim.opensandbox.svc.cluster.local:8888",
        description="Base URL OpenSandbox server (in-cluster Service).",
    )
    opensandbox_timeout_s: float = Field(default=120.0, ge=5.0, le=600.0)
    opensandbox_exec_path: str = Field(
        default="/api/v1/execute",
        description="POST path (append to base URL) — chỉnh theo spec upstream khi vendor manifest.",
    )
    opensandbox_default_image: str = Field(
        default="busybox:1.36",
        description="Image sandbox mặc định (multi-arch khi có).",
    )

    mcp_enabled: bool = Field(
        default=False,
        description="Pilot: MCP tool plane — off by default; see docs/mcp_integration.md.",
    )
    mcp_server_url: str = Field(
        default="",
        description="Optional MCP server base URL when mcp_enabled (placeholder until client ships).",
    )

    audit_sandbox_maxlen: int = Field(
        default=10_000,
        ge=1000,
        le=500_000,
        description="Retention hint (Kafka broker/topic policy; not enforced in app).",
    )
    audit_proactive_maxlen: int = Field(
        default=1000,
        ge=100,
        le=50_000,
        description="Retention hint for proactive audit topic.",
    )

    diagnostic_dictionary_enabled: bool = Field(
        default=True,
        description="SRE Diagnostic Dictionary: deterministic probes + Kafka evidence topic.",
    )
    diagnostic_matrix_path: str = Field(
        default="/app/config/diagnostic_matrix.yaml",
        validation_alias=AliasChoices("OMNI_DIAGNOSTIC_MATRIX_PATH"),
    )
    diagnostic_evidence_maxlen: int = Field(default=2000, ge=100, le=50_000)
    evidence_batch_agg_timeout_sec: float = Field(
        default=3.0,
        ge=0.5,
        le=120.0,
        validation_alias=AliasChoices("OMNI_EVIDENCE_BATCH_AGG_TIMEOUT_SEC"),
        description="Diagnostic evidence Redis batch: seconds to wait for probes before timeout flush.",
    )
    # Bách khoa K8s (pgvector k8s_expert) → luồng analyst sanitized (omni-diagnostic-evidence).
    diag_k8s_expert_rag_enabled: bool = Field(
        default=True,
        description="Gắn semantic search kubernetes.io (ingest) vào prompt khám khi evidence đã sanitized.",
    )
    diag_k8s_expert_rag_limit: int = Field(default=4, ge=1, le=16)
    diag_k8s_expert_rag_score_threshold: float = Field(default=0.40, ge=0.0, le=1.0)
    diag_k8s_expert_rag_max_chars: int = Field(default=3200, ge=400, le=16_000)
    diag_k8s_expert_rag_query_max_chars: int = Field(default=4000, ge=500, le=24_000)

    proactive_enabled: bool = Field(default=True, description="Prometheus evaluate + proactive incidents Kafka consumer.")
    proactive_gigo_require_cluster_identity: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_PROACTIVE_GIGO_REQUIRE_CLUSTER_IDENTITY"),
        description=(
            "GIGO: bỏ qua incident khi thiếu cả namespace và trigger_promql — tránh ReAct/mutate trên input không có định danh cluster."
        ),
    )
    proactive_kill_switch_key: str = Field(default="omni:proactive:kill_switch")
    proactive_eval_interval_sec: int = Field(default=120, ge=15, le=86400)
    proactive_promql: str = Field(
        default='sum(kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"})',
        description="Instant PromQL — scalar > threshold → enqueue AnomalyEvent (empty cluster → 0).",
    )
    proactive_trigger_threshold: float = Field(default=0.0, ge=0.0, description="Fire when instant query value > threshold.")
    proactive_promql_rules: str = Field(
        default="",
        description="JSON array [{\"name\":..,\"promql\":..,\"threshold\":..}, ...] — cho phép "
                    "theo dõi NHIỀU PromQL rule thay vì 1 rule hardcode duy nhất "
                    "(proactive_promql/proactive_trigger_threshold ở trên). Rỗng/không parse "
                    "được -> fallback nguyên rule đơn cũ (backward compat, KHÔNG breaking).",
    )
    proactive_cooldown_sec: int = Field(default=3600, ge=60, le=86400 * 7)
    consumer_group_proactive: str = Field(default="omni-worker-proactive")
    consumer_name_proactive: str = Field(default="omni-proactive-1")
    proactive_block_ms: int = Field(default=5000, ge=500)
    proactive_sop_collection: str = Field(default="itops_sop_ledger_v2")
    proactive_sop_score_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    proactive_fallback_enabled: bool = Field(
        default=True,
        description="When SOP miss in proactive path, call bounded LLM fallback with policy gate.",
    )
    proactive_fallback_max_attempts: int = Field(
        default=3,
        ge=1,
        le=4,
        description="Số lần thử parse JSON tool-call từ LLM (mỗi vòng ReAct có thể gọi lại).",
    )
    proactive_react_max_turns: int = Field(
        default=6,
        ge=1,
        le=24,
        validation_alias=AliasChoices("OMNI_PROACTIVE_REACT_MAX_TURNS"),
        description="Max vòng diagnose→prescribe→treat→recheck trong proactive fallback.",
    )
    proactive_negative_pattern_ttl_sec: int = Field(
        default=604800,
        ge=3600,
        le=86400 * 30,
        validation_alias=AliasChoices("OMNI_PROACTIVE_NEGATIVE_PATTERN_TTL_SEC"),
        description="TTL Redis omni:learning:negative:proactive:* — tránh lặp playbook xấu.",
    )
    proactive_fallback_confidence_min: float = Field(
        default=0.78,
        ge=0.0,
        le=1.0,
        description="Ngưỡng confidence LLM trong proactive fallback (thấp hơn khi cần tự xử lý incident đơn giản).",
    )
    proactive_fallback_allow_tools: str = Field(
        default=(
            "promql_instant,query_prometheus_metrics,k8s_list_pods,inspect_pod_deep,inspect_pod_details,"
            "list_namespace_pods,list_all_pods_sdk,resolve_pod_identity,resolve_deployment_identity,"
            "redis_health,redis_info,k8s_rollout_restart,k8s_scale_deployment,k8s_scale_resource,"
            "k8s_patch_resource,k8s_patch_configmap,k8s_patch_secret,k8s_create_or_patch_configmap,k8s_apply_rbac_least_privilege,k8s_delete_pod,"
            "k8s_describe_resource,k8s_tail_logs,k8s_get_logs,k8s_get_events,k8s_list_resources,k8s_check_endpoints,"
            "k8s_get_deployment_state,k8s_list_workload_pods,k8s_verify_rollout,kubectl_cluster,"
            "k8s_list_nodes,k8s_list_services,vendor_knowledge_search,k8s_expert_search"
        ),
        description="CSV tool allowlist khi OMNI_CLUSTER_FULL_ACCESS=false (mặc định true dùng full TOOL_REGISTRY).",
    )
    proactive_fallback_bypass_policy_in_god_mode: bool = Field(
        default=True,
        description="If god/lab mode, bypass fallback policy+confidence deny but still audit all actions.",
    )
    proactive_verify_keywords_fail: str = Field(
        default="error,exception,traceback,failed,forbidden,timeout,empty result,result rỗng",
        description="CSV keywords to classify quick post-check failure in proactive fallback.",
    )
    llm_chat_timeout_sec: float = Field(
        default=120.0,
        ge=5.0,
        le=300.0,
        validation_alias=AliasChoices("OMNI_LLM_TIMEOUT_SEC"),
        description=(
            "asyncio gates on llm.chat/embed AND AsyncOpenAI HTTP timeout "
            "(via build_llm_client); env OMNI_LLM_TIMEOUT_SEC."
        ),
    )
    telegram_send_timeout_sec: float = Field(
        default=10.0,
        ge=3.0,
        le=60.0,
        validation_alias=AliasChoices("OMNI_TELEGRAM_SEND_TIMEOUT_SEC"),
        description="asyncio.wait_for timeout for each Telegram send_message call.",
    )
    forecast_min_r_squared: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("OMNI_FORECAST_MIN_R_SQUARED"),
        description="Minimum r_squared for forecast to be considered reliable.",
    )
    forecast_ewma_alpha: float = Field(
        default=0.3,
        ge=0.01,
        le=0.99,
        validation_alias=AliasChoices("OMNI_FORECAST_EWMA_ALPHA"),
        description="EWMA level smoothing factor α (Holt's method). Higher = more reactive to recent data.",
    )
    forecast_ewma_beta: float = Field(
        default=0.1,
        ge=0.01,
        le=0.99,
        validation_alias=AliasChoices("OMNI_FORECAST_EWMA_BETA"),
        description="EWMA trend smoothing factor β (Holt's method). Lower = smoother trend, less micro-burst sensitivity.",
    )
    proactive_event_timeout_sec: float = Field(
        default=600.0,
        ge=30.0,
        le=7200.0,
        description="Wall-clock cap for entire _process_proactive_message (after semaphore acquire).",
    )
    proactive_tool_timeout_sec: float = Field(
        default=120.0,
        ge=5.0,
        le=1800.0,
        description="Per-tool asyncio.wait_for in proactive fallback.",
    )
    proactive_react_tool_output_max_chars: int | None = Field(
        default=None,
        ge=400,
        le=8000,
        description=(
            "Cap sanitize+truncate for proactive ReAct tool returns (verify, audit, react_mem); "
            "None uses tool_output_max_chars."
        ),
    )
    proactive_react_memory_max_chars: int = Field(
        default=2400,
        ge=400,
        le=8000,
        description="Max chars for react_memory block merged into proactive fallback prompt (tail kept).",
    )
    proactive_llm_prompt_max_chars: int = Field(
        default=3072,
        ge=800,
        le=12000,
        description="Max chars for full proactive fallback prompt before LLM parse (head kept).",
    )
    proactive_react_require_namespace_for_list: bool = Field(
        default=True,
        description="Proactive ReAct: cấm k8s_list_pods không có namespace (tránh quét cả cluster).",
    )
    proactive_react_memory_line_max_chars: int = Field(
        default=2000,
        ge=200,
        le=8000,
        description="Max chars per line stored in omni:proactive:react_mem:* (RPUSH).",
    )
    proactive_resource_freeze_enabled: bool = Field(
        default=True,
        description="Redis freeze key per (namespace, kind, name) after REQUIRES_HUMAN.",
    )
    proactive_resource_freeze_ttl_sec: int = Field(
        default=7200,
        ge=60,
        le=86400 * 7,
        description="TTL for omni:proactive:freeze:res:* keys.",
    )
    proactive_freeze_key_prefix: str = Field(
        default="omni:proactive:freeze:res",
        min_length=8,
        description="Prefix for resource-scoped freeze keys.",
    )
    proactive_freeze_namespace_fallback_allowed: bool = Field(
        default=False,
        description="If True, allow namespace-only freeze when kind/name cannot be extracted.",
    )
    proactive_k8s_snapshot_timeout_sec: float = Field(
        default=15.0,
        ge=2.0,
        le=120.0,
        description="fetch_last_known_state asyncio.wait_for cap.",
    )
    proactive_lease_ttl_sec: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Redis SET NX EX TTL for mutate lease (Phase 2).",
    )
    learning_governance_min_samples: int = Field(default=5, ge=1, le=1000)
    learning_governance_exec_lb95_min: float = Field(default=0.7, ge=0.0, le=1.0)
    learning_stats_ttl_sec: int = Field(default=86400 * 7, ge=3600, le=86400 * 365)
    reply_append_trace_id: bool = Field(
        default=False,
        description="Thêm trace_id vào cuối tin Telegram gửi user (stream_loop).",
    )
    action_experience_enabled: bool = Field(default=True)
    action_experience_score_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    multi_hypothesis_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_MULTI_HYPOTHESIS_ENABLED"),
        description="Tier-1 shadow: generate up to 3 hypotheses from local LLM (default off).",
    )
    multi_hypothesis_shadow_only: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_MULTI_HYPOTHESIS_SHADOW_ONLY"),
        description="Keep multi-hypothesis output in shadow artifacts only (no runtime decision override).",
    )
    deep_probe_orchestration_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_DEEP_PROBE_ORCHESTRATION_ENABLED"),
        description="Tier-1 shadow: attach probe suggestions from hypotheses (default off).",
    )
    knowledge_draft_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_KNOWLEDGE_DRAFT_ENABLED"),
        description="Stage Symptom/RootCause/Fix draft artifacts (no direct doc writes by default).",
    )
    omni_hitl_routing_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_HITL_ROUTING_ENABLED"),
        description=(
            "When True, escalation_reason advisories may route to omni-hitl-pending for HITL approval. "
            "Default False — all advisories remain suggest-only via Telegram. "
            "CRAT block HITL_ESCALATION_EMITTED is written BEFORE any Kafka send (fail-closed)."
        ),
    )
    omni_siem_suggest_only: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_SIEM_SUGGEST_ONLY"),
        description=(
            "When True (default), SIEM (FinGuard siem_source=finguard) evidence batches emit "
            "SUGGEST_REMEDIATION + Telegram to admin only — no EXECUTE_MUTATE and no HITL pipeline. "
            "Set to False only to re-enable autonomous execution for SIEM incidents (not recommended for prod)."
        ),
    )
    shadow_influence_suggest_only: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_SHADOW_INFLUENCE_SUGGEST_ONLY"),
        description="Tier-2 opt-in: shadow quality may improve suggest text only.",
    )
    autonomous_writeback_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_AUTONOMOUS_WRITEBACK_ENABLED"),
        description="Tier-2 opt-in: allow action_experience writeback path.",
    )
    knowledge_promotion_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_KNOWLEDGE_PROMOTION_ENABLED"),
        description="Tier-3 opt-in: allow reviewed draft promotion to docs.",
    )
    autodoc_git_push_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_AUTODOC_GIT_PUSH_ENABLED"),
        description="Tier-3 opt-in: allow doc git push only with explicit approval flow.",
    )
    routing_experience_enabled: bool = Field(
        default=True,
        description="Ghi + đọc Postgres action_experience để fast-path bỏ LLM sau slow-path thành công.",
    )
    routing_experience_score_threshold: float = Field(
        default=0.78,
        ge=0.35,
        le=0.99,
        description="Ngưỡng cosine Postgres cho hit routing (thường < SOP 0.9).",
    )
    routing_experience_max_chars: int = Field(default=4000, ge=500, le=8000)
    episodic_memory_enabled: bool = Field(
        default=False,
        description="Pha 1: false — chỉ upsert playbook; true có thể ghi thêm episodic (trace-scoped).",
    )
    memory_canonical_strip_pods: bool = Field(
        default=True,
        description="canonical_symptom_text: thay pod-like token bằng <pod> để khớp cross-incident.",
    )
    agentic_slow_path_enabled: bool = Field(
        default=False,
        description="ReAct multi-step slow-path; học chỉ khi omni_mark_resolved.",
    )
    agentic_max_llm_iterations: int = Field(default=8, ge=1, le=48)
    audit_agent_maxlen: int = Field(
        default=8000,
        ge=500,
        le=50_000,
        description="Retention hint for agentic audit topic.",
    )
    agentic_debug_io: bool = Field(
        default=False,
        description="Bật log JSON từng vòng agentic: llm_request (messages gửi Ollama) + llm_response (raw model output). Lab only.",
    )
    otel_tracing_enabled: bool = Field(
        default=False,
        description="Bật OTLP (BatchSpanProcessor) khi có endpoint.",
    )
    otel_exporter_otlp_endpoint: str = Field(
        default="",
        validation_alias=AliasChoices("OMNI_OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"),
        description="VD: http://tempo.monitor.svc.cluster.local:4317 (gRPC OTLP).",
    )
    otel_service_name: str = Field(
        default="omni-worker",
        validation_alias=AliasChoices("OMNI_OTEL_SERVICE_NAME", "OTEL_SERVICE_NAME"),
    )
    promotion_confidence_min: float = Field(default=0.95, ge=0.0, le=1.0)
    lesson_max_chars: int = Field(default=650, ge=200, le=2000)
    sandbox_log_clip_chars: int = Field(default=2000, ge=200, le=8000)
    write_pending_ttl_sec: int = Field(default=3600, ge=120, le=86400)
    fallback_inline_buttons_enabled: bool = Field(
        default=False,
        description="Fallback SRE: gửi 3 nút inline Telegram khi parse được SUGGESTIONS_JSON (mặc định tắt — tránh menu).",
    )

    # --- LAB / God mode (OMNI_LAB_UNCHAINED) — policy bypass, zero Telegram confirm, optional shell tool
    lab_unchained: bool = Field(
        default=False,
        description="LAB: bỏ denylist sandbox/promotion, bỏ CONFIRM_REQUIRED rollout, cho phép execute_shell_command.",
    )
    god_mode: bool = Field(
        default=False,
        description="Alias intent với lab_unchained (telemetry/logging).",
    )
    cluster_full_access: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_CLUSTER_FULL_ACCESS"),
        description=(
            "Vận hành cluster đầy đủ: kubectl_cluster, promotion→scale/patch/kubectl, proactive full toolbelt + bỏ gate confidence. "
            "Đặt OMNI_CLUSTER_FULL_ACCESS=false để khóa (chỉ rollout allowlist legacy)."
        ),
    )
    scout_synth_backend: str = Field(
        default="ollama",
        description="autonomous scout synthesize: ollama | gemini.",
    )
    agent_reasoning_backend: str = Field(
        default="ollama",
        description="Reasoning layer: ollama | gemini (Gemini cần GEMINI_API_KEY).",
    )
    fallback_llm_backend: str = Field(
        default="ollama",
        description="conversational_fallback: ollama | gemini.",
    )
    gemini_model: str = Field(default="gemini-2.0-flash", description="Model id Gemini Developer API.")
    gemini_max_retries: int = Field(default=4, ge=1, le=12)
    gemini_retry_base_delay_sec: float = Field(default=2.0, ge=0.5, le=60.0)
    gemini_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("GEMINI_API_KEY", "OMNI_GEMINI_API_KEY"),
        description="Optional; thường inject qua K8s Secret GEMINI_API_KEY (không commit).",
    )
    autonomous_bigbang_ingest: bool = Field(
        default=False,
        description="LAB: kubectl cluster JSON dump → synthesize chunks → Postgres (bật cùng lab).",
    )
    autonomous_bigbang_max_json_mb: int = Field(default=80, ge=5, le=500)
    autonomous_bigbang_replace_loops: bool = Field(
        default=False,
        description="Nếu bigbang ingest >0 chunk, bỏ vòng pod/service per-entity (tiết kiệm quota).",
    )
    ingest_secrets_raw: bool = Field(
        default=False,
        description="LAB_ONLY: nếu true, chunk có thể chứa Secret/ConfigMap nhạy cảm — cực kỳ nguy hiểm.",
    )

    # Prometheus metrics exporter (omni-worker pod)
    metrics_listen_host: str = Field(default="0.0.0.0")
    metrics_listen_port: int = Field(default=9090, ge=1024, le=65535)
    metrics_redis_stream_keys: str = Field(
        default="",
        validation_alias=AliasChoices("OMNI_METRICS_REDIS_STREAM_KEYS"),
        description=(
            "Comma-separated Redis Streams keys for XLEN→omni_redis_stream_backlog gauge "
            "(lab: e.g. stream:actionable_incidents)."
        ),
    )
    monitor_stack_namespace: str = Field(
        default="monitor",
        min_length=1,
        description="Namespace hosting Prometheus/Loki/Grafana (audit_observability_stack).",
    )

    # SOP bulk ingest (`python -m training.sop_ingest`) — seed → vLLM embed → Postgres
    max_sop_contexts: int = Field(
        default=10_000,
        ge=1,
        le=500_000,
        description="Cap số điểm SOP sau expand (round-robin templates).",
    )
    sop_seed_path: str = Field(
        default="data/sop/sop_templates.yaml",
        description="Đường dẫn YAML seed; trong image Docker: /app/data/sop/sop_templates.yaml.",
    )
    sop_expand_seed: int | None = Field(
        default=None,
        description="Optional RNG seed — shuffle thứ tự ingest (id point không đổi).",
    )
    training_llm_concurrency: int = Field(default=2, ge=1, le=8)
    sop_ingest_upsert_batch: int = Field(default=128, ge=8, le=512)
    sop_ingest_embed_batch: int = Field(default=32, ge=1, le=128)
    sop_ingest_log_every: int = Field(default=500, ge=1, le=50_000)

    # Vendor knowledge (`python -m knowledge.ingest_main`) — clean → chunk → embed → vendor_knowledge
    knowledge_sources_path: str = Field(
        default="/app/config/knowledge_sources.yaml",
        validation_alias=AliasChoices("OMNI_KNOWLEDGE_SOURCES"),
    )
    knowledge_ingest_embed_batch: int = Field(default=16, ge=1, le=128)
    knowledge_ingest_concurrency: int = Field(default=2, ge=1, le=8)

    # Pre-execute snapshot + auto-rollback (S1.2)
    omni_auto_rollback_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_AUTO_ROLLBACK_ENABLED"),
        description="Auto-restore K8s resource to pre-mutate state when verify is exhausted.",
    )
    omni_rollback_snapshot_ttl_sec: int = Field(
        default=3600,
        ge=60,
        le=86400,
        validation_alias=AliasChoices("OMNI_ROLLBACK_SNAPSHOT_TTL_SEC"),
        description="TTL for pre-mutate snapshots stored in Redis.",
    )

    # Cross-incident alert deduplication (S1.1)
    alert_dedup_window_sec: int = Field(
        default=300,
        ge=0,
        le=3600,
        validation_alias=AliasChoices("OMNI_ALERT_DEDUP_WINDOW_SEC"),
        description=(
            "Cross-incident dedup window (seconds). Same (source:alertname:namespace:deployment) "
            "within this window → skip pipeline to prevent alert storm. Set 0 to disable."
        ),
    )

    # Blast-Radius Diff-Scoring (plan step 3) — code-hard impact-tree gate at executor.
    omni_blast_radius_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_BLAST_RADIUS_ENABLED"),
        description="Enable blast-radius impact-tree gate before EXECUTE_MUTATE (lab opt-in).",
    )
    omni_blast_max_pods: int = Field(
        default=10,
        ge=1,
        le=10000,
        validation_alias=AliasChoices("OMNI_BLAST_MAX_PODS"),
        description="Hard-block a mutate whose impact-tree affects more than this many pods.",
    )
    omni_blast_capacity_drop_pct: float = Field(
        default=20.0,
        ge=1.0,
        le=100.0,
        validation_alias=AliasChoices("OMNI_BLAST_CAPACITY_DROP_PCT"),
        description="Hard-block a scale-down cutting replica capacity by ≥ this percentage.",
    )

    # DLQ rollup alerting (plan step 2) — accumulate failures into time-window buckets and
    # emit ONE Telegram notification per window instead of per-trace (anti alert-fatigue).
    dlq_rollup_window_sec: int = Field(
        default=60,
        ge=5,
        le=3600,
        validation_alias=AliasChoices("OMNI_DLQ_ROLLUP_WINDOW_SEC"),
        description="Time-window (seconds) for DLQ rollup notifications.",
    )

    # Ingress QoS storm control (plan step 1) — NORMAL-priority alerts are admitted
    # via an atomic sliding window; CRITICAL/SIEM are never shed. 0 disables shedding.
    alert_qos_normal_cap: int = Field(
        default=500,
        ge=0,
        le=100000,
        validation_alias=AliasChoices("OMNI_ALERT_QOS_NORMAL_CAP"),
        description=(
            "Max NORMAL-priority alerts admitted per QoS window. Excess is shed (Critical/"
            "SIEM never shed). Set 0 to disable QoS shedding entirely."
        ),
    )
    alert_qos_window_sec: int = Field(
        default=60,
        ge=1,
        le=3600,
        validation_alias=AliasChoices("OMNI_ALERT_QOS_WINDOW_SEC"),
        description="Sliding-window length (seconds) for NORMAL-priority alert admission.",
    )

    # RAG freshness + DEPRECATED_RISK resolution (plan step 4) — recalled chunks carry
    # cluster_version/ingested_at; on drift vs live cluster they are forced to re-verify.
    omni_rag_freshness_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OMNI_RAG_FRESHNESS_ENABLED"),
        description="Enable RAG chunk freshness/DEPRECATED_RISK drift gate at recall (lab opt-in).",
    )
    omni_rag_freshness_max_age_sec: int = Field(
        default=2_592_000,  # 30 days
        ge=3600,
        le=31_536_000,
        validation_alias=AliasChoices("OMNI_RAG_FRESHNESS_MAX_AGE_SEC"),
        description="Age (seconds) beyond which a version-matched chunk is labelled STALE (soft).",
    )
    omni_cluster_version: str = Field(
        default="",
        validation_alias=AliasChoices("OMNI_CLUSTER_VERSION"),
        description="Live K8s cluster version stamped onto RAG chunks and compared at recall.",
    )

    # Dynamic LLM context window (S2.1)
    llm_num_ctx: int = Field(
        default=8192,
        ge=512,
        le=131072,
        validation_alias=AliasChoices("OMNI_LLM_NUM_CTX"),
        description=(
            "Ollama num_ctx for main LLM calls. "
            "4096 keeps KV cache fully in GPU/unified memory on M4 (no swap). "
            "Increase only if evidence texts regularly exceed 3k tokens."
        ),
    )
    proactive_llm_num_ctx: int = Field(
        default=4096,
        ge=2048,
        le=131072,
        validation_alias=AliasChoices("OMNI_PROACTIVE_LLM_NUM_CTX"),
        description=(
            "Ollama num_ctx for proactive observer and forecast LLM calls. "
            "Lower than main num_ctx to save VRAM on high-frequency probes."
        ),
    )

    # Forecast → Proactive Pipeline Integration (S2.3)
    forecast_proactive_integration_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_FORECAST_PROACTIVE_INTEGRATION_ENABLED"),
        description=(
            "When True, forecast threshold breaches emit to omni-proactive-incidents "
            "AND set an elevated watch key so proactive observer doubles its check frequency."
        ),
    )

    # Auto-Promote SOP Pipeline (S2.2)
    omni_sop_auto_promote_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_SOP_AUTO_PROMOTE_ENABLED"),
        description="Enable automatic SOP promotion after repeated VERIFIED_SUCCESS on same pattern.",
    )
    omni_sop_promotion_min_success: int = Field(
        default=3,
        ge=1,
        le=100,
        validation_alias=AliasChoices("OMNI_SOP_PROMOTION_MIN_SUCCESS"),
        description="Minimum VERIFIED_SUCCESS count before auto-promoting to SOP ledger.",
    )
    omni_sop_promotion_max_fp_rate: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("OMNI_SOP_PROMOTION_MAX_FP_RATE"),
        description="Maximum global false-positive rate allowed for SOP promotion.",
    )
    omni_sop_promotion_require_fp_data: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_SOP_PROMOTION_REQUIRE_FP_DATA"),
        description=(
            "Fail-closed: refuse auto_execute SOP promotion when there is not enough "
            "KPI data to compute a false-positive rate. Set false only in a lab where "
            "promoting without quality evidence is acceptable."
        ),
    )
    omni_sop_skill_export_dir: str = Field(
        default="",
        validation_alias=AliasChoices("OMNI_SOP_SKILL_EXPORT_DIR"),
        description=(
            "Directory for exporting promoted SOPs as markdown skills. Empty (default) "
            "disables the export entirely."
        ),
    )

    omni_capacity_advisor_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OMNI_CAPACITY_ADVISOR_ENABLED"),
        description=(
            "Bật loop capacity advisory + báo cáo SRE định kỳ (chỉ đọc & publish, "
            "không mutate gì)."
        ),
    )

    # Advisory graduation (G1) — vòng học chạy được trong shadow/advisory mode
    omni_advisory_graduation_min_success: int = Field(
        default=3,
        ge=1,
        le=100,
        validation_alias=AliasChoices("OMNI_ADVISORY_GRADUATION_MIN_SUCCESS"),
        description="Số lần operator ack liên tiếp trước khi advisory pattern tốt nghiệp.",
    )
    omni_advisory_graduation_max_fail_rate: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("OMNI_ADVISORY_GRADUATION_MAX_FAIL_RATE"),
        description="Tỉ lệ reject tối đa; vượt ngưỡng thì playbook bị FROZEN (mất bậc).",
    )

    # HITL Fallback Channel + Dead-Letter Queue (S1.3)
    omni_hitl_fallback_channel: str = Field(
        default="none",
        validation_alias=AliasChoices("OMNI_HITL_FALLBACK_CHANNEL"),
        description=(
            "Secondary escalation channel when HITL has no decision after escalation_timeout. "
            "Supported: 'slack' | 'none'. Extend hitl_fallback.py for pagerduty/email."
        ),
    )
    omni_hitl_fallback_webhook_url: str = Field(
        default="",
        validation_alias=AliasChoices("OMNI_HITL_FALLBACK_WEBHOOK_URL"),
        description="Slack incoming webhook URL for HITL escalation fallback.",
    )
    omni_hitl_escalation_timeout_sec: int = Field(
        default=900,
        ge=60,
        le=3600,
        validation_alias=AliasChoices("OMNI_HITL_ESCALATION_TIMEOUT_SEC"),
        description=(
            "Seconds after HITL registration before emitting to fallback channel. "
            "Must be less than HITL_APPROVAL_TIMEOUT_SEC (default 1800)."
        ),
    )
    omni_hitl_dead_letter_ttl_sec: int = Field(
        default=86400,
        ge=3600,
        le=604800,
        validation_alias=AliasChoices("OMNI_HITL_DEAD_LETTER_TTL_SEC"),
        description="TTL for timed-out HITL actions stored in Redis dead-letter hash (seconds).",
    )
