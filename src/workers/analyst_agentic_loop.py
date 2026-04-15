"""Up to N LLM steps: optional read-only ReAct, then a single EXECUTE_MUTATE plan (tool_name + args) when RAG misses."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from pkg.reasoning.deterministic_mutate_from_evidence import _evidence_suggests_credential_failure
from pkg.reasoning.diagnostic_policy import DISCOVERY_TOOL_ALIASES, evidence_suggests_broken_spec
from pkg.reasoning.incident_matrix_profile import VALID_PROOF_LANES, pick_matrix_row_for_batch
from pkg.reasoning.reason_codes import (
    ERR_REA_HALLUCINATION_DETECTED,
    ERR_REA_SCHEMA_VIOLATION,
    ERR_SEM_CHANNEL_MISMATCH,
    PLANNER_PHASE_DONE,
)
from workers.autonomous_execute import MUTATE_TOOL_ALLOWLIST, READONLY_TOOL_ALLOWLIST
from workers.evidence_mutate_emit import rollout_args_from_evidence_batch
from workers.llm_trace import agentic_parse_failure_hint, log_llm_trace
from workers.memory.initial_symptom import InitialSymptom
from workers.memory.trace_memory import (
    ActionRecord,
    OmniTraceMemory,
    format_trace_memory_block,
    load_trace_memory,
    save_trace_memory,
    truncate_for_action_record,
)
from workers.post_verify_deployment_state import sanitize_probe_text_for_llm
from workers.tool_registry import get_tool_registry

logger = logging.getLogger(__name__)

_POST_VERIFY_REACT_MEM_KEY = "omni:post_verify_react_mem:{trace}"
_POD_SCOPED_READONLY_TOOLS = frozenset(
    {
        "k8s_get_logs",
        "k8s_get_pod_log_tail",
        "k8s_get_pod_log_previous",
        "k8s_get_pod_secret_refs",
    }
)

# Explicit LLM tool catalog: security remediation mutates (subset of MUTATE_TOOL_ALLOWLIST; always surfaced in prompt).
LLM_TOOL_CATALOG_SECURITY_MUTATE = (
    "Explicit mutate tool catalog (security):\n"
    "- k8s_apply_rbac_least_privilege — scope executor SA to least-privilege Role/RoleBinding; "
    "optional remove_cluster_admin_binding.\n"
    "- k8s_patch_configmap — patch one key in a ConfigMap data map; requires namespace, name, key, value.\n"
    "- k8s_patch_secret — rotate/restore a single key in a K8s Secret via stringData; "
    "requires namespace, name, key, value, reasoning. "
    "Use this when an app is in CrashLoop due to stale or rotated credentials.\n"
)

# Explicit LLM tool catalog: ConfigMap remediation mutates.
LLM_TOOL_CATALOG_CONFIGMAP_MUTATE = (
    "Explicit mutate tool catalog (ConfigMap fix):\n"
    "- k8s_create_or_patch_configmap — idempotent create-or-update a ConfigMap key; "
    "requires namespace, name, key, value. "
    "Use this when a ConfigMap is CONFIRMED ABSENT (FailedMount / CreateContainerConfigError). "
    "Do NOT use k8s_rollout_restart alone when the ConfigMap never existed — "
    "restart cannot create a missing ConfigMap.\n"
    "- k8s_patch_configmap — patch one key in an EXISTING ConfigMap; "
    "use only when the ConfigMap already exists.\n"
)

# System prompt: Observation-first + 4-stage ReAct reasoning (English, JSON-only output).
_REACT_SYSTEM = (
    "You are Omni SRE. Strictly observation-first: never guess causes before listing facts from the Fact Table.\n"
    "CRITICAL RULE: K8s Pods are ephemeral. NEVER rely on them as static targets. "
    "Always trace issues back to the parent Workload KIND (Deployment, StatefulSet, etc.) using label selectors. "
    "If a specific pod is missing, query the cluster for the current pods managed by that workload.\n"
    "HARD POLICY: Pod-scoped diagnostic calls are forbidden for planning/remediation decisions. "
    "Use workload identity (namespace + deployment/statefulset) and workload-scoped tools only.\n"
    "ABSOLUTE CONSTRAINTS:\n"
    "- Reasoning MUST be concise (max 3 sentences in the 'thought' field). "
    "Focus strictly on factual evidence from the Fact Table. "
    "Do NOT hallucinate, speculate, or over-explain. "
    "You MUST return a specific tool call immediately — an output with no tool_name is INVALID "
    "unless phase is exactly 'done' (see EVIDENCE-BASED TERMINATION below).\n"
    "STRICT SCHEMA: Every response MUST be a single valid JSON object. Required keys:\n"
    '  {"thought": "<max 3 sentences of factual reasoning>", '
    '"decision": "<discovery|mutate|done|escalate>", '
    '"tool_name": "<exact tool name from allowlist or empty for done/escalate>", '
    '"args": {<tool arguments>}, '
    '"phase": "<observe|verify|remediate|done>", '
    '"step": "<readonly|mutate>"}\n'
    "Optional keys: \"working_hypothesis\": \"<short update when your hypothesis changes>\", "
    '"missing_preconditions": ["<what evidence is missing before mutate>"], '
    '"resolution_summary": "<required when phase is done — see below>", '
    '"escalation_reason": "<required when decision=escalate>".\n'
    "TRACE_MEMORY (XML block appears in the user message):\n"
    "- When <INITIAL_SYMPTOM> is present, it is the structured Prometheus/Alertmanager alert snapshot for this trace; "
    "compare read-only tool lines in <HISTORY> against it when judging recovery or phase \"done\".\n"
    "- You MUST read <HISTORY> inside <TRACE_MEMORY> before choosing the next action.\n"
    "- You MUST NOT re-run the same tool with identical arguments if that call already appears in <HISTORY> "
    "with is_error=true or an error-like result_summary, or if it did not yield new information — pick a different "
    "read-only probe, refine args, set phase to done, or propose mutate with new justification.\n"
    "EVIDENCE-BASED TERMINATION (phase \"done\"):\n"
    "- This planner runs read-only tools in-process; EXECUTE_MUTATE is emitted separately. "
    "You MUST NOT set phase to \"done\" in the same JSON as a mutate tool call (step mutate with a non-empty "
    "mutate tool_name). After you propose a mutate, verification of recovery happens in a later "
    "planner run with fresh evidence — do not claim success in the same turn as a mutate proposal.\n"
    "- You MAY set phase to \"done\" only when read-only tool output already recorded in <HISTORY> (and/or the "
    "Fact Table) supports that the incident is resolved relative to initial_symptoms / initial narrative — "
    "compare explicitly; do not claim recovery without citing that evidence.\n"
    "- Post-mutation (when <EXECUTOR_FEEDBACK> or POST-MUTATE STATE VERIFICATION is present): you MUST judge "
    "real-world recovery from the fresh Fact Table and probe lines — e.g. workload Ready/Running signals vs "
    "CrashLoopBackOff, and whether fresh logs still show the prior FATAL/root error. kubectl/executor success alone is insufficient.\n"
    "- When phase is \"done\", include \"resolution_summary\": a concise paragraph stating what evidence shows "
    "recovery vs the original failure mode (no fabricated success).\n"
    'STRICT RULES:\n'
    "1) 'thought': max 3 sentences, grounded ONLY in Fact Table evidence. No speculation.\n"
    "2) 'tool_name': MUST be non-empty when decision is discovery/mutate. Pick read-only tools first.\n"
    "3) 'args': MUST be a JSON object (use {} only when tool truly needs no fields).\n"
    "4) Never emit a mutating tool before running at least one read-only discovery tool.\n"
    "5) Read-only tools are always executed as discovery at runtime regardless of step field.\n"
    "6) Do NOT include markdown code fences (```json) or any text outside the JSON object.\n"
    "When phase is 'done': set tool_name to empty string, args to {}, step to 'mutate' or 'readonly' as required, "
    "and provide resolution_summary. Use thought/analysis for reasoning trace.\n"
    "response_format: json_object is enforced — output ONLY the JSON object, nothing else."
)

_POST_MUTATE_STATE_VERIFY_APPEND = (
    "\n\nPOST-MUTATE STATE VERIFICATION (mandatory when this block is active):\n"
    "- The executor has already applied a mutation and reported exit_code=0. That is TASK success only, not STATE success.\n"
    "- Use <TRACE_MEMORY> <HISTORY> to see prior actions; fresh evidence after cooldown is in the Fact Table and "
    "[EXECUTOR_FEEDBACK] / sdk_probe_summary_after_delay.\n"
    "- When deciding recovery, you MUST compare fresh evidence to the original failure mode (<INITIAL_SYMPTOM> when present).\n"
    "- Only phase \"done\" if real-world evidence supports recovery (e.g. prior crash/auth FATAL pattern absent in fresh facts, "
    "workload health consistent with resolution). If the failure mode still appears in fresh probes/logs, do NOT return done — "
    "propose read-only discovery, a different mutate, or abstain so the system can escalate.\n"
)


def _react_system_content(ws: Any, *, post_mutate_verify: bool = False) -> str:
    """Base ReAct system prompt plus optional sole-evaluator instructions."""
    base = _REACT_SYSTEM
    if post_mutate_verify:
        base = base + _POST_MUTATE_STATE_VERIFY_APPEND
    if bool(getattr(ws, "omni_planner_llm_sole_evaluator", False)):
        base += (
            "\nSOLE EVALUATOR MODE: You alone decide whether the incident is resolved. "
            "Before setting phase to \"done\", compare <INITIAL_SYMPTOM> (when present) to the latest "
            "read-only tool outputs in <HISTORY>. Do not claim recovery without citing that comparison.\n"
        )
    return base


_POST_VERIFY_REACT_SYSTEM = (
    _REACT_SYSTEM
    + "\n\nSPECIAL SESSION — post-mutate deployment gate:\n"
    "- SDK probes already reported PASSED; the Kubernetes Deployment API still shows unhealthy rollout.\n"
    "- Identity anchor: ONLY namespace + Deployment name. Never target a specific Pod name for remediation.\n"
    "- Multi-round ReAct: each user message includes Session memory (thoughts + prior observations). "
    "Use it for CoT — do not repeat stale guesses; refine based on new observations.\n"
    "- Prefer read-only inspection of Deployment / ReplicaSet / Events / pod list by label selector before mutating.\n"
)


def _normalize_describe_resource_type(value: str | None) -> str | None:
    """Map LLM kind/resource_type strings to DescribeResourceArgs Literal (Pod|Deployment|Service|ConfigMap|Secret)."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    low = s.lower()
    if low in ("pod", "pods"):
        return "Pod"
    if low in ("deployment", "deployments"):
        return "Deployment"
    if low in ("service", "services"):
        return "Service"
    if low in ("configmap", "configmaps"):
        return "ConfigMap"
    if low in ("secret", "secrets"):
        return "Secret"
    if s in ("Pod", "Deployment", "Service", "ConfigMap", "Secret"):
        return s
    return None


def coerce_k8s_readonly_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """
    Best-effort arg normalization before readonly tool execution (LLM often sends kind vs resource_type).
    For k8s_describe_resource, resource_type is normalized to one of:
    Pod | Deployment | Service | ConfigMap | Secret.
    For k8s_get_deployment_state / k8s_verify_rollout, LLM often sends name= instead of deployment=.
    """
    out = dict(args or {})
    if tool_name in ("k8s_get_deployment_state", "k8s_verify_rollout", "k8s_list_workload_pods"):
        if not out.get("deployment") and out.get("name"):
            out["deployment"] = out.pop("name")
            logger.info(
                "event=k8s_args_coerced tool=%s field=name->deployment value=%s",
                tool_name,
                out["deployment"],
            )
        return out
    if tool_name != "k8s_describe_resource":
        return out
    out = dict(args or {})
    rt_raw = out.get("resource_type")
    norm = _normalize_describe_resource_type(rt_raw) if isinstance(rt_raw, str) else None
    if norm:
        out["resource_type"] = norm
        for k in ("kind", "resource_kind", "api_kind"):
            out.pop(k, None)
        return out
    for key in ("kind", "resource_kind", "api_kind"):
        raw = out.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        stripped = raw.strip()
        cand = _normalize_describe_resource_type(stripped)
        if cand:
            out["resource_type"] = cand
            out.pop(key, None)
            for k2 in ("kind", "resource_kind", "api_kind"):
                out.pop(k2, None)
            logger.info(
                "event=k8s_args_coerced tool=k8s_describe_resource field=%s to_resource_type=%s",
                key,
                cand,
            )
            return out
    return out


def _readonly_tool_router(tool_name: str) -> bool:
    """Runtime router: if tool is read-only, it never goes to mutate/executor semantics."""
    return bool(tool_name and str(tool_name).strip() in READONLY_TOOL_ALLOWLIST)


def _discovery_repeat_dedupe_key(tool_name: str, args: dict[str, Any]) -> str:
    """Anti-loop key for the LLM's requested readonly step (stable across pod→workload redirect)."""
    return _json_fingerprint({"t": str(tool_name or "").strip(), "a": dict(args or {})})


def _repeat_key_from_record(rec: ActionRecord) -> str:
    if getattr(rec, "repeat_dedupe_key", None):
        return str(rec.repeat_dedupe_key)
    return _discovery_repeat_dedupe_key(rec.tool_name, rec.args)


def _infer_workload_identity(
    batch: list[dict[str, Any]], sanitized_text: str
) -> tuple[str, str]:
    """Best-effort namespace/deployment identity inferred from alert/evidence."""
    rollout = rollout_args_from_evidence_batch(batch)
    if rollout:
        ns = str(rollout.get("namespace") or "").strip()
        dep = str(rollout.get("deployment") or "").strip()
        if ns and dep:
            return ns, dep
    text = sanitized_text or ""
    m_ns = re.search(r"\bnamespace=([a-z0-9-]+)\b", text, flags=re.IGNORECASE)
    m_dep = re.search(r"\bdeployment=([a-z0-9-]+)\b", text, flags=re.IGNORECASE)
    ns = (m_ns.group(1) if m_ns else "").strip()
    dep = (m_dep.group(1) if m_dep else "").strip()
    return ns, dep


def _is_pod_scoped_readonly_request(tool_name: str, args: dict[str, Any]) -> bool:
    tn = str(tool_name or "").strip()
    if tn in _POD_SCOPED_READONLY_TOOLS:
        return True
    if tn == "k8s_get_events":
        involved_name = str(args.get("involved_name") or "").strip()
        involved_kind = str(args.get("involved_kind") or "Pod").strip().lower()
        if involved_name and (involved_kind in ("", "pod")):
            return True
    if tn != "k8s_describe_resource":
        return False
    rt = _normalize_describe_resource_type(
        str(
            args.get("resource_type")
            or args.get("kind")
            or args.get("resource_kind")
            or args.get("api_kind")
            or ""
        )
    )
    return rt == "Pod"


def _rewrite_pod_scoped_to_workload_probe(
    tool_name: str,
    args: dict[str, Any],
    *,
    namespace: str,
    deployment: str,
) -> tuple[str, dict[str, Any], str]:
    """Enforce workload-scoped diagnostics when model asks for pod-scoped tools."""
    ns = str(namespace or args.get("namespace") or "").strip()
    dep = str(deployment or args.get("deployment") or args.get("name") or "").strip()
    if ns and dep:
        return (
            "k8s_get_deployment_state",
            {"namespace": ns, "deployment": dep},
            f"pod_scoped_blocked:{tool_name}->k8s_get_deployment_state",
        )
    if ns:
        return (
            "k8s_list_resources",
            {"resource": "deployments", "namespace": ns},
            f"pod_scoped_blocked:{tool_name}->k8s_list_resources",
        )
    return (
        "k8s_list_resources",
        {"resource": "deployments", "namespace": "multi-agent"},
        f"pod_scoped_blocked:{tool_name}->k8s_list_resources",
    )


def _planner_model_candidates(ws: Any) -> list[str]:
    vals = [
        str(getattr(ws, "diag_evidence_llm_model", "") or "").strip(),
        str(getattr(ws, "model_reasoning_engine", "") or "").strip(),
        str(getattr(ws, "model_helper", "") or "").strip(),
        str(getattr(ws, "chat_model", "") or "").strip(),
    ]
    out: list[str] = []
    for v in vals:
        if v and v not in out:
            out.append(v)
    return out


def build_fact_table_prompt(batch: list[dict[str, Any]], sanitized_text: str) -> str:
    """Structured facts JSON for the planner (reduces raw log noise)."""
    rows: list[dict[str, Any]] = []
    for b in batch[:16]:
        probe = str(b.get("probe") or "")
        ef = b.get("extracted_fact")
        if isinstance(ef, dict):
            ef_s = json.dumps(ef, ensure_ascii=False)[:4000]
        elif isinstance(ef, str):
            ef_s = ef[:4000]
        else:
            ef_s = str(ef or "")[:4000]
        rows.append(
            {
                "probe": probe[:240],
                "alert_rule": str(b.get("alert_rule") or "")[:240],
                "alert_hint": str(b.get("alert_hint") or "")[:800],
                "extracted_fact_excerpt": ef_s,
            }
        )
    blob = json.dumps({"facts": rows}, ensure_ascii=False)[:12000]
    return (
        "Fact table (JSON):\n"
        f"{blob}\n\n"
        "Sanitized narrative (truncated):\n"
        f"{sanitized_text[:4500]}\n"
    )


def _planner_tool_catalog_prompt(max_chars: int = 12000) -> str:
    """Machine-readable tool catalog (args schema + preconditions metadata)."""
    reg = get_tool_registry()
    return reg.tool_catalog_json_for_prompt(max_chars=max_chars)


_RE_CM_NOT_FOUND = re.compile(r'configmap\s+"([^"]+)"\s+not\s+found', re.IGNORECASE)
_RE_SECRET_NOT_FOUND = re.compile(r'secret\s+"([^"]+)"\s+not\s+found', re.IGNORECASE)


def _batch_text_for_planner_hints(batch: list[dict[str, Any]]) -> str:
    """Compact text for regex hints (raw + extracted_fact) — same signals as broken-spec policy."""
    parts: list[str] = []
    for b in batch:
        parts.append(str(b.get("raw") or ""))
        ef = b.get("extracted_fact")
        if isinstance(ef, dict):
            parts.append(json.dumps(ef, ensure_ascii=False))
        elif isinstance(ef, str):
            parts.append(ef)
    return "\n".join(parts)


def _general_credential_failure_hint(batch: list[dict[str, Any]]) -> str:
    """
    When evidence shows DB/API authentication failure (password auth failed, etc.),
    inject a general directive so the LLM picks k8s_patch_secret — NOT rollout_restart.
    No secret values are included; the LLM must read the correct value from the cluster/runbook.
    """
    if not _evidence_suggests_credential_failure(batch):
        return ""
    ns = ""
    for b in batch:
        ef = b.get("extracted_fact")
        if isinstance(ef, dict):
            cand = str(ef.get("namespace") or "").strip()
            if cand:
                ns = cand
                break
        # also check raw alert fields
        if not ns:
            for field in ("namespace", "alert_hint"):
                cand = str(b.get(field) or "").strip()
                if cand and "/" not in cand and len(cand) < 64:
                    ns = cand
                    break
    lines = [
        "CREDENTIAL FAILURE DETECTED (DB/API authentication failure in pod logs):",
        "- Root cause: K8s Secret contains stale or wrong credentials.",
        "- Rollout restart CANNOT fix this — the Secret value must be corrected first.",
        "- Required tool: k8s_patch_secret",
        "- Arg keys needed: namespace, name (Secret name from pod env secretKeyRef),",
        "  key (env var key from pod spec / logs), value (correct credential — read from cluster or ops runbook),",
        "  reasoning.",
        f"- Namespace from evidence: {ns or '(take from Fact table / probe blocks)'}.",
        "- Do NOT call k8s_rollout_restart as the primary fix step.",
        "- If unsure of the Secret name: use k8s_describe_resource on the failing pod first to read secretKeyRef.\n",
    ]
    return "\n".join(lines) + "\n\n"


def _broken_spec_first_round_instruction(batch: list[dict[str, Any]]) -> str:
    """
    When evidence shows FailedMount / missing ConfigMap|Secret, the LLM often returns
    phase=observe + analysis only (no tool_name) → _reject_reason → ERR_REA_SCHEMA_VIOLATION, tool=na.
    Inject explicit tool + names from pod events so round-1 JSON matches the executor contract.
    """
    if not evidence_suggests_broken_spec(batch):
        return ""
    t = _batch_text_for_planner_hints(batch)
    cm_m = _RE_CM_NOT_FOUND.search(t)
    sec_m = _RE_SECRET_NOT_FOUND.search(t)
    ns = ""
    for b in batch:
        ef = b.get("extracted_fact")
        if isinstance(ef, dict):
            cand = str(ef.get("namespace") or "").strip()
            if cand:
                ns = cand
                break
    lines = [
        "BROKEN-SPEC PRIORITY (this trace — mount / missing ConfigMap or Secret):",
        '- Invalid: JSON with only "phase" and "analysis" and no "tool_name".',
        '- Required each round until done: "tool_name", "args" (object), "step":"readonly" or "mutate",',
        '  plus "phase" and "analysis". For discovery use tool_name=k8s_describe_resource.',
        "- Describe the missing ConfigMap or Secret before proposing rollout_restart or patches.",
        f"- Namespace from evidence: {ns or '(take from Fact table / probe blocks)'}.\n",
        "- After confirming the ConfigMap is ABSENT (404 / not found in describe output):",
        "  use tool_name=k8s_create_or_patch_configmap with args namespace, name, key, value.",
        "  Do NOT use k8s_rollout_restart as the only fix — restart cannot create a missing ConfigMap.",
        "  If a follow-up rollout is needed after creating the CM, emit it in a subsequent step.\n",
    ]
    if cm_m:
        name = cm_m.group(1)
        lines.append(
            f'- Example round-1 JSON: {{"tool_name":"k8s_describe_resource",'
            f'"args":{{"resource_type":"ConfigMap","name":"{name}","namespace":"{ns or "NAMESPACE"}}},'
            '"step":"readonly","phase":"verify","analysis":"Confirm ConfigMap absent or misconfigured."}}'
        )
        lines.append(
            f'- Example round-2 JSON (after confirm absent): {{"tool_name":"k8s_create_or_patch_configmap",'
            f'"args":{{"namespace":"{ns or "NAMESPACE"}","name":"{name}","key":"placeholder","value":"created-by-omni",'
            f'"reasoning":"ConfigMap {name} was absent; creating with placeholder key to unblock pod mount."}},'
            '"step":"mutate","phase":"remediate","analysis":"ConfigMap confirmed absent; creating it now."}}'
        )
    elif sec_m:
        name = sec_m.group(1)
        lines.append(
            f'- Example round-1 JSON: {{"tool_name":"k8s_describe_resource",'
            f'"args":{{"resource_type":"Secret","name":"{name}","namespace":"{ns or "NAMESPACE"}}},'
            '"step":"readonly","phase":"verify","analysis":"Confirm Secret absent or misconfigured."}}'
        )
    else:
        lines.append(
            "- Parse FailedMount / CreateContainerConfigError in the Fact table for the resource name; "
            "then call k8s_describe_resource for that ConfigMap or Secret."
        )
    return "\n".join(lines) + "\n\n"


def _parse_agentic_json(raw: str) -> dict[str, Any] | None:
    s = (raw or "").strip()
    if not s:
        return None
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        o = json.loads(s[i : j + 1])
        return o if isinstance(o, dict) else None
    except Exception:
        return None


def _json_fingerprint(value: Any) -> str:
    """Stable short hash for log correlation (prompt/tool args replay detection)."""
    try:
        blob = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        blob = str(value)
    return hashlib.sha256(blob.encode("utf-8", errors="ignore")).hexdigest()[:12]


# Backward compat: ``autonomous_feedback_loop`` imports ``_parse_tool_json``.
_parse_tool_json = _parse_agentic_json


def _infer_readonly_error(obs: str) -> bool:
    o = (obs or "").strip().lower()
    if "[data] error" in o or "unknown_readonly_tool" in o:
        return True
    if o.startswith("[data] error"):
        return True
    return False


def _compact_batch_hint(batch: list[dict[str, Any]]) -> str:
    try:
        rows = [{"probe": b.get("probe"), "result": str(b.get("result") or "")[:120]} for b in batch[:10]]
        return json.dumps(rows, ensure_ascii=False)[:1800]
    except Exception:
        return ""


def _initial_symptoms_for_memory(sanitized_text: str, batch: list[dict[str, Any]]) -> str:
    st = (sanitized_text or "").strip()
    if st:
        return st[:2000]
    return _compact_batch_hint(batch)


def _reject_reason(parsed: dict[str, Any] | None) -> str:
    if not parsed:
        return ERR_REA_SCHEMA_VIOLATION
    tn = str(parsed.get("tool_name") or "").strip()
    if not tn:
        return ERR_REA_SCHEMA_VIOLATION
    if tn in READONLY_TOOL_ALLOWLIST:
        return ERR_SEM_CHANNEL_MISMATCH
    if tn not in MUTATE_TOOL_ALLOWLIST:
        return ERR_REA_HALLUCINATION_DETECTED
    args = parsed.get("args")
    if not isinstance(args, dict):
        args = parsed.get("tool_args")
    if not isinstance(args, dict):
        return ERR_REA_SCHEMA_VIOLATION
    if tn == "k8s_rollout_restart":
        ns = str(args.get("namespace") or "").strip()
        dep = str(args.get("deployment") or "").strip()
        if not ns or not dep:
            return ERR_REA_SCHEMA_VIOLATION
    if tn in ("k8s_patch_configmap", "k8s_create_or_patch_configmap", "k8s_patch_secret"):
        for k in ("namespace", "name", "key"):
            if not str(args.get(k) or "").strip():
                return ERR_REA_SCHEMA_VIOLATION
        if "value" not in args:
            return ERR_REA_SCHEMA_VIOLATION
    if tn == "k8s_patch_resource":
        if not str(args.get("namespace") or "").strip() or not str(args.get("name") or "").strip():
            return ERR_REA_SCHEMA_VIOLATION
        pj = str(args.get("patch_json") or "").strip()
        if len(pj) < 2:
            return ERR_REA_SCHEMA_VIOLATION
    return ""


def _post_verify_namespace_bound_ok(parsed: dict[str, Any], *, bound_ns: str, bound_dep: str) -> bool:
    """Mutate args must stay within the workload identity (namespace + deployment for rollout)."""
    args = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
    if not args and isinstance(parsed.get("tool_args"), dict):
        args = parsed.get("tool_args") or {}
    if str(args.get("namespace") or "").strip() != (bound_ns or "").strip():
        return False
    tn = str(parsed.get("tool_name") or "").strip()
    if tn == "k8s_rollout_restart":
        if str(args.get("deployment") or "").strip() != (bound_dep or "").strip():
            return False
    return True


def _truncate_obs(text: str, cap: int) -> str:
    s = (text or "").strip()
    return s if len(s) <= cap else s[: cap - 1] + "…"


def _planner_readonly_output_cap(ws: Any) -> int:
    """Use the larger of generic tool cap and trace-memory cap so <HISTORY> retains enough text."""
    t = int(getattr(ws, "tool_output_max_chars", 1500) or 1500) if ws is not None else 1500
    m = int(getattr(ws, "omni_trace_memory_tool_output_max_chars", 4000) or 4000) if ws is not None else 4000
    return max(400, max(t, m))


async def _execute_readonly_tool(
    ctx: Any,
    tool_name: str,
    args: dict[str, Any],
    *,
    output_max_chars: int | None = None,
) -> str:
    from workers.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        return f"[DATA] error\n[DIAGNOSIS] unknown_readonly_tool name={tool_name!r}"
    ws = getattr(ctx, "settings", None)
    if output_max_chars is not None:
        cap = max(400, int(output_max_chars))
    else:
        cap = int(getattr(ws, "tool_output_max_chars", 1500) or 1500) if ws is not None else 1500
        cap = max(400, cap)
    try:
        out = await fn(ctx, dict(args or {}))
        return _truncate_obs(str(out), cap)
    except Exception as e:
        logger.warning("readonly_tool_failed name=%s err=%s", tool_name, e)
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


async def infer_blind_proof_lane_hint(
    ctx: Any,
    batch: list[dict[str, Any]],
    *,
    sanitized_text: str,
    rag_match_text: str | None,
) -> str | None:
    """
    When the incident matrix does not match, optionally ask local LLM for proof_lane (resource|state|app_log).
    """
    if pick_matrix_row_for_batch(batch, rag_match_text=rag_match_text) is not None:
        return None
    ws = getattr(ctx, "settings", None)
    llm = getattr(ctx, "llm", None)
    if ws is None or llm is None:
        return None
    if not bool(getattr(ws, "omni_blind_lane_llm_enabled", False)):
        return None
    model_candidates = _planner_model_candidates(ws)
    if not model_candidates:
        return None
    hint_txt = (sanitized_text or "")[:3000]
    messages = [
        {"role": "system", "content": "Reply with exactly one word: resource, state, or app_log. No punctuation."},
        {
            "role": "user",
            "content": f"Pick proof lane for diagnostic evidence (three lanes: resource, state, app_log).\n{hint_txt}",
        },
    ]
    for model in model_candidates:
        try:
            resp = await llm.chat(model=model, messages=messages, stream=False)
            msg = (resp or {}).get("message") or {}
            raw = str(msg.get("content") or "").strip().lower()
            m = re.search(r"\b(resource|state|app_log)\b", raw)
            if m:
                w = m.group(1)
                if w in VALID_PROOF_LANES:
                    return w
        except Exception as e:
            logger.debug("blind_lane_llm model=%s err=%s", model, e)
    return None


def _format_post_mutate_verify_user_block(pm: dict[str, Any]) -> str:
    """User-message prefix for executor feedback + fresh probe summary (state-success gate)."""
    tool = str(pm.get("tool_name") or "")
    stdout = str(pm.get("stdout") or "")
    vsum = str(pm.get("verify_summary") or "")
    sdk_ok = pm.get("sdk_all_passed")
    return (
        "\n[POST-MUTATE STATE VERIFICATION — read before Fact Table]\n"
        "<EXECUTOR_FEEDBACK>\n"
        f"exit_code={int(pm.get('exit_code') or 0)}\n"
        f"tool_name={tool}\n"
        "stdout_excerpt:\n"
        f"{stdout[:2800]}\n"
        "</EXECUTOR_FEEDBACK>\n"
        "<FRESH_PROBE_SUMMARY_AFTER_DELAY>\n"
        f"{vsum[:4000]}\n"
        "</FRESH_PROBE_SUMMARY_AFTER_DELAY>\n"
        f"sdk_all_passed_programmatic_hint={sdk_ok!r} "
        "(hint only — you must still judge recovery from facts; do not trust exit_code alone).\n\n"
    )


async def run_agentic_mutate_plan(
    ctx: Any,
    *,
    trace: str,
    sanitized_text: str,
    batch: list[dict[str, Any]],
    max_steps: int,
    rag_reasoning_hints: str | None = None,
    recall_prefix: str | None = None,
    initial_symptom: InitialSymptom | None = None,
    post_mutate_verify: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Returns mutate plan {tool_name, args, discovery_steps?, reasoning_chain?, lane_hint?} or None.

    When OMNI_DIAGNOSTIC_REACT_ENABLED: interleave read-only tool JSON until a mutate JSON is returned.

    rag_reasoning_hints: optional RAG chunk text (reference only) when early SUGGEST was suppressed.
    initial_symptom: optional structured Prometheus alert; persisted in trace memory for <INITIAL_SYMPTOM>.
    """
    ws = getattr(ctx, "settings", None)
    llm = getattr(ctx, "llm", None)
    if ws is None or llm is None:
        return None
    model_candidates = _planner_model_candidates(ws)
    if not model_candidates:
        logger.warning("event=agentic_mutate_plan_no_model trace=%s", trace)
        return None
    react_on = bool(getattr(ws, "omni_diagnostic_react_enabled", False))
    ro_max = max(0, int(getattr(ws, "omni_diagnostic_react_readonly_max", 3) or 3))
    r_redis = getattr(ctx, "redis", None)
    mem_out_cap = int(getattr(ws, "omni_trace_memory_tool_output_max_chars", 4000) or 4000)
    ro_cap = _planner_readonly_output_cap(ws)
    fact_block = build_fact_table_prompt(batch, sanitized_text)
    probe_list = [str(x.get("probe")) for x in batch[:12]]
    discovery_steps: list[str] = []
    thought_process: list[str] = []

    rag_block = ""
    if (rag_reasoning_hints or "").strip():
        rag_block = (
            "\nRAG reference hints (not verified — use read-only tools to confirm):\n"
            f"{str(rag_reasoning_hints).strip()[:12000]}\n\n"
        )

    sole_eval = bool(getattr(ws, "omni_planner_llm_sole_evaluator", False))
    llm_first = bool(getattr(ws, "omni_llm_first_autonomy_enabled", False))
    legacy_prompt_hints = (not sole_eval) and (not llm_first)
    broken_prefix = _broken_spec_first_round_instruction(batch) if legacy_prompt_hints else ""
    credential_hint = _general_credential_failure_hint(batch) if legacy_prompt_hints else ""
    # recall_prefix: high-confidence playbook recall takes precedence, then credential hint, then broken_spec
    priority_prefix = recall_prefix or credential_hint or broken_prefix

    pm_block = _format_post_mutate_verify_user_block(post_mutate_verify) if post_mutate_verify else ""
    repeat_guard_threshold = 2
    repeat_guard_hits = 0
    forced_decision_instruction = ""
    workload_ns, workload_dep = _infer_workload_identity(batch, sanitized_text)

    base_user = (
        f"{priority_prefix}"
        f"{pm_block}"
        f"{fact_block}\n"
        f"{rag_block}"
        f"Probes: {probe_list}\n\n"
        "Stages (Observation-first): OBSERVATION (fact table) -> HYPOTHESIS (>=2) -> "
        "Verification (read-only) -> Final Verdict (mutate or abstain).\n"
        "If a hypothesis is not yet confirmable from facts, your next JSON MUST be a Discovery (read-only) tool, "
        "not a mutate.\n"
        "Reply with exactly one JSON object per round.\n"
        'For read-only inspection: decision="discovery", "tool_name" (read-only), "args" (object), "step":"readonly".\n'
        'When ready to mutate after verification: decision="mutate", "tool_name" (mutate allowlist), "args", "step":"mutate".\n'
        'If mutate is not yet safe: set decision="discovery" and provide "missing_preconditions".\n'
        'If escalation is required: decision="escalate", empty tool_name/args, include "escalation_reason".\n'
        "Evidence-based exit: when read-only verification in <TRACE_MEMORY> / Fact Table shows the issue is "
        "addressed vs initial_symptoms, use phase:\"done\", empty tool_name, and "
        '"resolution_summary" (what evidence proves recovery). Example: '
        '{"decision":"done","phase":"done","thought":"...","resolution_summary":"Describe/logs show ... vs initial ...",'
        '"tool_name":"","args":{},"step":"readonly"}.\n'
        "MUTATE PRECONDITION RULE: when decision=mutate you MUST ensure required metadata preconditions "
        "from TOOL_CATALOG_WITH_PRECONDITIONS are present in TRACE_MEMORY/HISTORY or Fact Table; otherwise return "
        "decision=discovery with missing_preconditions.\n"
        "WORKLOAD-FIRST DIAGNOSTICS POLICY: infer namespace+deployment/statefulset from alert labels/history and "
        "run only workload-scoped diagnostics. Do NOT call pod-scoped tools "
        "(k8s_get_logs, k8s_get_pod_log_previous, k8s_get_pod_secret_refs, or describe Pod). "
        "If you need pod-level symptoms, obtain them through workload-scoped probes and selectors.\n"
        f"{LLM_TOOL_CATALOG_SECURITY_MUTATE}"
        f"{LLM_TOOL_CATALOG_CONFIGMAP_MUTATE}"
        f"Mutate allowlist (full): {', '.join(sorted(MUTATE_TOOL_ALLOWLIST))}.\n"
        f"Read-only examples: {', '.join(sorted(list(READONLY_TOOL_ALLOWLIST))[:12])}...\n"
        "k8s_describe_resource args MUST use resource_type one of: Pod, Deployment, Service, ConfigMap, Secret (exact casing); "
        "plus name and namespace. Example: "
        '{"tool_name":"k8s_describe_resource","args":{"resource_type":"ConfigMap","name":"nginx-config","namespace":"lab-test"},'
        '"step":"readonly"}. '
        "If you use kind instead of resource_type, runtime maps kind→resource_type for all five kinds. "
        "For broken-spec faults (FailedMount, missing ConfigMap/Secret), describe the ConfigMap or Secret first.\n"
        "For k8s_rollout_restart, args must include namespace and deployment.\n"
        'For k8s_create_or_patch_configmap (PREFERRED when CM is absent): {"namespace","name","key","value"} — e.g. '
        '{"tool_name":"k8s_create_or_patch_configmap","args":{"namespace":"multi-agent","name":"nginx-test-never-created-cm",'
        '"key":"placeholder","value":"created-by-omni","reasoning":"CM was absent; creating to unblock pod mount"},"step":"mutate"}.\n'
        'For k8s_patch_configmap: {"namespace","name","key","value"} — e.g. '
        '{"tool_name":"k8s_patch_configmap","args":{"namespace":"multi-agent","name":"omni-worker-config",'
        '"key":"OMNI_GOD_MODE","value":"false","reasoning":"prod guard"},"step":"mutate"}.\n'
        'For k8s_apply_rbac_least_privilege: optional namespace, executor_sa, remove_cluster_admin_binding; '
        '{"tool_name":"k8s_apply_rbac_least_privilege","args":{"namespace":"multi-agent"},'
        '"step":"mutate"}.\n'
        'For k8s_patch_secret: {"namespace","name","key","value","value_source","value_source_ref"} — e.g. '
        '{"tool_name":"k8s_patch_secret","args":{"namespace":"multi-agent","name":"<must come from k8s_get_pod_secret_refs>",'
        '"key":"DB_PASSWORD","value":"<from source-of-truth>","value_source":"runbook|lab_env|human_confirmed",'
        '"value_source_ref":"ticket-or-doc-id","reasoning":"restore credential mismatch"},"step":"mutate"}.\n'
        "Never guess secret name; discover it from k8s_get_pod_secret_refs before k8s_get_secret_keys/k8s_patch_secret.\n"
        "For k8s_patch_resource (Deployment), args must include namespace, name, patch_json (object as JSON string).\n"
        "TOOL_CATALOG_WITH_PRECONDITIONS (JSON):\n"
        f"{_planner_tool_catalog_prompt(max_chars=12000)}\n"
    )

    total = max(1, int(max_steps))
    ro_budget = max(ro_max, 1)  # always allow at least one readonly redirect from mistaken mutate JSON
    sys_content = _react_system_content(ws, post_mutate_verify=bool(post_mutate_verify))
    mem: OmniTraceMemory | None = None
    for step in range(total):
        # Redis authoritative when present; without Redis keep one in-process OmniTraceMemory for the loop.
        if r_redis is not None:
            mem = await load_trace_memory(
                r_redis,
                trace,
                initial_symptoms=_initial_symptoms_for_memory(sanitized_text, batch),
                initial_symptom=initial_symptom,
                seed_attempt=0,
            )
        elif mem is None:
            mem = await load_trace_memory(
                None,
                trace,
                initial_symptoms=_initial_symptoms_for_memory(sanitized_text, batch),
                initial_symptom=initial_symptom,
                seed_attempt=0,
            )
        assert mem is not None
        trace_block = format_trace_memory_block(mem)
        user_content = (
            base_user
            + forced_decision_instruction
            + "\nThe <TRACE_MEMORY> block below is authoritative session state for this trace_id.\n"
            + trace_block
            + f"\n(round {step + 1}/{total})\n"
        )
        if step == 0:
            log_llm_trace(
                ws,
                trace=trace,
                phase="agentic_mutate_plan_prompt_contract",
                step=1,
                parse_ok=True,
                detail=(
                    f"react_on={react_on} ro_budget={ro_budget} max_steps={total} "
                    f"tool_catalog_included=True system_len={len(sys_content)} user_len={len(user_content)} "
                    f"system_sha={_json_fingerprint(sys_content)} user_sha={_json_fingerprint(user_content)}"
                ),
                raw_response=(
                    "[SYSTEM_PROMPT_EXCERPT]\n"
                    f"{sys_content[:2400]}\n\n"
                    "[USER_PROMPT_EXCERPT]\n"
                    f"{user_content[:2400]}"
                ),
            )
        else:
            log_llm_trace(
                ws,
                trace=trace,
                phase="agentic_mutate_plan_prompt_round",
                step=step + 1,
                parse_ok=True,
                detail=(
                    f"user_len={len(user_content)} user_sha={_json_fingerprint(user_content)} "
                    f"trace_history_actions={len(mem.action_history)}"
                ),
                raw_response=f"{user_content[:1800]}",
            )

        messages = (
            [
                {"role": "system", "content": sys_content},
                {"role": "user", "content": user_content},
            ]
            if react_on
            else [
                {
                    "role": "system",
                    "content": sys_content
                    + " When OMNI_DIAGNOSTIC_REACT_ENABLED is off, you still follow the same four stages; "
                    "output one JSON object per step.",
                },
                # Must use user_content (not base_user alone): prior read-only tool output lives in obs_tail.
                {"role": "user", "content": user_content},
            ]
        )
        try:
            parsed_any: dict[str, Any] | None = None
            last_content = ""
            for model in model_candidates:
                try:
                    resp = await llm.chat(
                        model=model, messages=messages, stream=False, format="json"
                    )
                except Exception as me:
                    logger.warning("agentic step %s model=%s: %s", step + 1, model, me)
                    log_llm_trace(
                        ws,
                        trace=trace,
                        phase="agentic_mutate_plan_chat_error",
                        model=str(model),
                        step=step + 1,
                        detail=str(me)[:800],
                    )
                    continue
                msg = (resp or {}).get("message") or {}
                content = str(msg.get("content") or "")
                last_content = content
                parsed_any = _parse_agentic_json(content)
                if parsed_any is None:
                    log_llm_trace(
                        ws,
                        trace=trace,
                        phase="agentic_mutate_plan",
                        model=str(model),
                        step=step + 1,
                        raw_response=content,
                        parse_ok=False,
                        parse_hint=agentic_parse_failure_hint(content),
                    )
                else:
                    log_llm_trace(
                        ws,
                        trace=trace,
                        phase="agentic_mutate_plan",
                        model=str(model),
                        step=step + 1,
                        raw_response=content,
                        parse_ok=True,
                        parsed_tool=str(parsed_any.get("tool_name") or "").strip() or None,
                    )
                if parsed_any:
                    break
            if not parsed_any:
                log_llm_trace(
                    ws,
                    trace=trace,
                    phase="agentic_mutate_plan_no_parse_all_models",
                    step=step + 1,
                    raw_response=last_content,
                    parse_ok=False,
                    parse_hint=agentic_parse_failure_hint(last_content),
                )
                continue

            step_kind = str(parsed_any.get("step") or "").strip().lower()
            tn = str(parsed_any.get("tool_name") or "").strip()
            # Accept both "args" (legacy) and "tool_args" (new schema).
            args_raw = parsed_any.get("args") or parsed_any.get("tool_args")
            args = args_raw if isinstance(args_raw, dict) else {}
            ph = str(parsed_any.get("phase") or "").strip().lower()
            decision = str(parsed_any.get("decision") or "").strip().lower()
            if decision not in {"discovery", "mutate", "done", "escalate"}:
                if ph == "done":
                    decision = "done"
                elif tn in READONLY_TOOL_ALLOWLIST:
                    decision = "discovery"
                elif tn:
                    decision = "mutate"
            args_fp = _json_fingerprint(args)
            log_llm_trace(
                ws,
                trace=trace,
                phase="agentic_mutate_plan_parse_labels",
                model=model_candidates[0] if model_candidates else None,
                step=step + 1,
                parse_ok=True,
                detail=(
                    f"decision={decision or 'na'} phase={ph or 'na'} step_field={step_kind or 'na'} "
                    f"tool={tn or 'na'} args_keys={','.join(sorted([str(k) for k in args.keys()])) or 'none'} "
                    f"args_sha={args_fp} missing_preconditions="
                    f"{json.dumps(parsed_any.get('missing_preconditions') if isinstance(parsed_any.get('missing_preconditions'), list) else [], ensure_ascii=False)[:500]}"
                ),
            )
            analysis_line = str(parsed_any.get("thought") or parsed_any.get("analysis") or "").strip()
            if analysis_line:
                thought_process.append(f"[{ph or 'step'}] {analysis_line}")

            wh = str(parsed_any.get("working_hypothesis") or "").strip()
            if wh:
                mem.working_hypothesis = wh[:500]
                await save_trace_memory(r_redis, mem)

            if decision == "escalate":
                esc_reason = str(parsed_any.get("escalation_reason") or analysis_line or "planner_requested_escalation")
                out_esc: dict[str, Any] = {
                    "reason_code": "PLANNER_REQUESTED_ESCALATION",
                    "phase": "escalate",
                    "decision": "escalate",
                    "tool_name": "",
                    "args": {},
                    "final_analysis": esc_reason[:2000],
                }
                mem.action_history.append(
                    ActionRecord(
                        step=step + 1,
                        tool_name="",
                        args={},
                        result_summary=truncate_for_action_record(f"Escalate: {esc_reason}", max_chars=mem_out_cap),
                        is_error=False,
                        kind="phase_done",
                    )
                )
                mem.attempt_count = len(mem.action_history)
                await save_trace_memory(r_redis, mem)
                return out_esc

            if decision == "done" or ph == "done":
                if tn.strip():
                    logger.warning(
                        "event=agentic_mutate_plan_reject_done_with_tool trace=%s tool=%s",
                        trace,
                        tn,
                    )
                    thought_process.append("schema: phase=done requires empty tool_name")
                    continue
                res_txt = str(parsed_any.get("resolution_summary") or "").strip()
                fa = res_txt or analysis_line or "Planner concluded; no automated mutate proposed."
                pl_hint_done = str(parsed_any.get("proof_lane_hint") or "").strip().lower()
                lane_done = pl_hint_done if pl_hint_done in VALID_PROOF_LANES else "unknown"
                tp_done: list[str] = list(thought_process)
                rc_done: dict[str, Any] = {
                    "verdict": "SUGGEST_FIX",
                    "lane": lane_done,
                    "thought_process": tp_done,
                    "resolution_summary": res_txt,
                }
                logger.info(
                    "event=agentic_mutate_plan_done trace=%s step=%s discovery=%s",
                    trace,
                    step + 1,
                    discovery_steps,
                )
                out_done: dict[str, Any] = {
                    "reason_code": PLANNER_PHASE_DONE,
                    "phase": "done",
                    "decision": "done",
                    "tool_name": "",
                    "args": {},
                    "final_analysis": fa,
                    "resolution_summary": res_txt,
                    "discovery_steps": discovery_steps,
                    "reasoning_chain": rc_done,
                }
                if pl_hint_done in VALID_PROOF_LANES:
                    out_done["lane_hint"] = pl_hint_done
                mem.working_hypothesis = (
                    f"Resolved: {fa}"[:500] if fa else mem.working_hypothesis
                )
                mem.action_history.append(
                    ActionRecord(
                        step=step + 1,
                        tool_name="",
                        args={},
                        result_summary=truncate_for_action_record(fa, max_chars=mem_out_cap),
                        is_error=False,
                        kind="phase_done",
                    )
                )
                mem.attempt_count = len(mem.action_history)
                await save_trace_memory(r_redis, mem)
                return out_done

            # Router: read-only tools always → discovery (fixes model choosing step=mutate with inspect/describe).
            if decision == "discovery" or _readonly_tool_router(tn):
                if not tn:
                    thought_process.append("discovery_requested_but_tool_missing")
                    continue
                # LLM request fingerprint (before pod→workload redirect) — avoids false repeats when
                # multiple pod-scoped tools map to the same workload-scoped execution.
                repeat_dedupe_key = _discovery_repeat_dedupe_key(tn, dict(args))
                if _is_pod_scoped_readonly_request(tn, args):
                    new_tn, new_args, reason = _rewrite_pod_scoped_to_workload_probe(
                        tn,
                        args,
                        namespace=workload_ns,
                        deployment=workload_dep,
                    )
                    thought_process.append(reason)
                    logger.info(
                        "event=pod_scoped_readonly_blocked trace=%s step=%s tool=%s redirect=%s",
                        trace,
                        step + 1,
                        tn,
                        new_tn,
                    )
                    tn = new_tn
                    args = new_args
                args_fp = _json_fingerprint(args)
                same_calls = 0
                for rec in mem.action_history:
                    if _repeat_key_from_record(rec) == repeat_dedupe_key:
                        same_calls += 1
                if same_calls >= 1:
                    log_llm_trace(
                        ws,
                        trace=trace,
                        phase="agentic_mutate_plan_gigo_repeat",
                        model=model_candidates[0] if model_candidates else None,
                        step=step + 1,
                        parse_ok=True,
                        detail=(
                            f"label=repeat_discovery_tool tool={tn} args_sha={args_fp} "
                            f"repeat_key={repeat_dedupe_key[:16]} "
                            f"repeat_count={same_calls + 1} action_history={len(mem.action_history)}"
                        ),
                    )
                if same_calls + 1 >= repeat_guard_threshold:
                    repeat_guard_hits += 1
                    forced_decision_instruction = (
                        "\nOMNI_GUARD: You already requested this read-only call with identical args "
                        f"at least {repeat_guard_threshold} times. This is an infinite loop risk. "
                        "You MUST now either: "
                        '1) emit decision="mutate" with complete args and provenance fields, or '
                        '2) emit decision="escalate" with escalation_reason. '
                        "DO NOT repeat this discovery call again.\n\n"
                    )
                    guard_summary = (
                        f"loop_guard_triggered tool={tn} args_sha={args_fp} repeat_key={repeat_dedupe_key[:16]} "
                        f"repeat_count={same_calls + 1}"
                    )
                    thought_process.append(guard_summary)
                    mem.action_history.append(
                        ActionRecord(
                            step=step + 1,
                            tool_name=tn,
                            args=dict(args),
                            repeat_dedupe_key=repeat_dedupe_key,
                            result_summary=truncate_for_action_record(
                                f"[DATA] loop_guard_blocked\n[DIAGNOSIS] {guard_summary}",
                                max_chars=mem_out_cap,
                            ),
                            is_error=True,
                            kind="readonly_executed",
                        )
                    )
                    mem.attempt_count = len(mem.action_history)
                    await save_trace_memory(r_redis, mem)
                    log_llm_trace(
                        ws,
                        trace=trace,
                        phase="agentic_mutate_plan_loop_guard_forced_decision",
                        model=model_candidates[0] if model_candidates else None,
                        step=step + 1,
                        parse_ok=True,
                        detail=guard_summary,
                    )
                    if repeat_guard_hits >= 2:
                        logger.warning(
                            "event=agentic_mutate_plan_loop_guard_abort trace=%s step=%s tool=%s",
                            trace,
                            step + 1,
                            tn,
                        )
                        break
                    continue
                if len(discovery_steps) >= ro_budget:
                    thought_process.append("readonly_max_steps_reached")
                    continue
                exec_args = coerce_k8s_readonly_args(tn, dict(args))
                obs = await _execute_readonly_tool(ctx, tn, exec_args, output_max_chars=ro_cap)
                discovery_steps.append(tn)
                is_err = _infer_readonly_error(obs)
                mem.action_history.append(
                    ActionRecord(
                        step=step + 1,
                        tool_name=tn,
                        args=dict(exec_args),
                        repeat_dedupe_key=repeat_dedupe_key,
                        result_summary=truncate_for_action_record(obs, max_chars=mem_out_cap),
                        is_error=is_err,
                        kind="readonly_executed",
                    )
                )
                mem.attempt_count = len(mem.action_history)
                await save_trace_memory(r_redis, mem)
                thought_process.append(
                    f"Verification (read-only {tn}): excerpt in trace memory; "
                    f"model_step={step_kind or 'n/a'}."
                )
                logger.info(
                    "event=readonly_discovery_redirect trace=%s step=%s tool=%s",
                    trace,
                    step + 1,
                    tn,
                )
                continue

            # mutate branch (only non-read-only tools reach here)
            parsed = parsed_any
            reason = _reject_reason(parsed)
            if reason:
                rejected_tool = str(parsed.get("tool_name") or "").strip()
                logger.info(
                    "event=agentic_mutate_plan_reject trace=%s step=%s reason_code=%s tool=%s",
                    trace,
                    step + 1,
                    reason,
                    rejected_tool or "na",
                )
                log_llm_trace(
                    ws,
                    trace=trace,
                    phase="agentic_mutate_plan_reject",
                    step=step + 1,
                    parse_ok=True,
                    parsed_tool=rejected_tool or None,
                    reject_reason=reason,
                    detail=json.dumps(parsed, ensure_ascii=False, default=str)[:2500],
                )
                continue
            tn = str(parsed.get("tool_name") or "").strip()
            args_raw = parsed.get("args")
            if not isinstance(args_raw, dict):
                args_raw = parsed.get("tool_args")
            args = args_raw if isinstance(args_raw, dict) else {}
            pl_hint = str(parsed_any.get("proof_lane_hint") or "").strip().lower()
            lane_hint: str | None = None
            if pl_hint in VALID_PROOF_LANES:
                lane_hint = pl_hint
            tp: list[str] = list(thought_process) if thought_process else []
            if not tp:
                tp = [f"Planned mutate tool {tn}"] if tn else ["Empty plan"]
            tp.insert(
                0,
                "OBSERVATION: Facts from Fact Table and prior rounds; no mutate until verification when required.",
            )
            if discovery_steps:
                tp.append(f"VERIFICATION: read-only discovery tools executed: {', '.join(discovery_steps)}")
            tp.append(
                f"FINAL_VERDICT: {'EXECUTE_PLAN' if tn else 'EMPTY'} mutate_tool={tn or 'none'} "
                f"(discovery_steps={len(discovery_steps)})"
            )
            rc = {
                "verdict": "EXECUTE_PLAN" if tn else "EMPTY",
                "lane": lane_hint or "unknown",
                "thought_process": tp,
            }
            logger.info(
                "event=agentic_mutate_plan_ok trace=%s step=%s tool=%s discovery=%s",
                trace,
                step + 1,
                tn,
                discovery_steps,
            )
            out: dict[str, Any] = {
                "decision": "mutate",
                "tool_name": tn,
                "args": dict(args),
                "discovery_steps": discovery_steps,
                "reasoning_chain": rc,
                "missing_preconditions": parsed_any.get("missing_preconditions")
                if isinstance(parsed_any.get("missing_preconditions"), list)
                else [],
            }
            if lane_hint:
                out["lane_hint"] = lane_hint
            mem.action_history.append(
                ActionRecord(
                    step=step + 1,
                    tool_name=tn,
                    args=dict(args),
                    result_summary="Mutate plan emitted; executor runs separately.",
                    is_error=False,
                    kind="mutate_planned",
                )
            )
            mem.attempt_count = len(mem.action_history)
            await save_trace_memory(r_redis, mem)
            return out
        except Exception as e:
            logger.warning("agentic step %s: %s", step + 1, e)

    logger.info("event=agentic_mutate_plan_fail trace=%s", trace)
    return None


async def run_post_mutate_state_verify_planner(
    ctx: Any,
    *,
    trace: str,
    batch: list[dict[str, Any]],
    sanitized_text: str,
    stdout: str,
    tool_name: str,
    verify_summary: str,
    sdk_all_passed: bool,
    initial_symptom: InitialSymptom | None = None,
) -> dict[str, Any] | None:
    """
    Sole-evaluator re-entry after executor exit 0: fresh Fact Table + executor excerpt.
    Returns same shape as run_agentic_mutate_plan (phase done, or mutate plan, or None).
    """
    ws = getattr(ctx, "settings", None)
    if ws is None:
        return None
    max_steps = int(getattr(ws, "omni_post_mutate_state_verify_max_steps", 6) or 6)
    out = await run_agentic_mutate_plan(
        ctx,
        trace=trace,
        sanitized_text=sanitized_text,
        batch=batch,
        max_steps=max_steps,
        rag_reasoning_hints=None,
        recall_prefix=None,
        initial_symptom=initial_symptom,
        post_mutate_verify={
            "exit_code": 0,
            "stdout": stdout,
            "tool_name": tool_name,
            "verify_summary": verify_summary,
            "sdk_all_passed": sdk_all_passed,
        },
    )
    logger.info(
        "event=post_mutate_state_verify_planner_done trace=%s phase=%s tool=%s",
        trace,
        (out or {}).get("phase"),
        (out or {}).get("tool_name"),
    )
    return out


async def _persist_post_verify_memory(
    ctx: Any,
    trace: str,
    *,
    thought_process: list[str],
    observations: list[str],
    step_idx: int,
) -> None:
    r = getattr(ctx, "redis", None)
    if r is None:
        return
    key = _POST_VERIFY_REACT_MEM_KEY.format(trace=trace)
    try:
        await r.setex(
            key,
            7200,
            json.dumps(
                {
                    "round": step_idx,
                    "thoughts": thought_process[-24:],
                    "observations": [o[:2000] for o in observations[-16:]],
                },
                ensure_ascii=False,
            ),
        )
    except Exception as e:
        logger.debug("post_verify_react mem persist: %s", e)


async def _clear_post_verify_memory(ctx: Any, trace: str) -> None:
    r = getattr(ctx, "redis", None)
    if r is None:
        return
    try:
        await r.delete(_POST_VERIFY_REACT_MEM_KEY.format(trace=trace))
    except Exception:
        pass


async def run_post_verify_react_loop(
    ctx: Any,
    *,
    trace: str,
    namespace: str,
    deployment: str,
    verify_summary: str,
    dep_detail: str,
    executor_stdout: str,
) -> dict[str, Any] | None:
    """
    Multi-step ReAct + CoT when SDK probes pass but Deployment rollout is still unhealthy.
    Short-term memory = in-process thought_process + observations, persisted to Redis each round.
    Returns mutate plan {tool_name, args, reasoning_chain, ...} or None.
    """
    ws = getattr(ctx, "settings", None)
    llm = getattr(ctx, "llm", None)
    if ws is None or llm is None:
        return None
    if not bool(getattr(ws, "omni_post_verify_state_llm_enabled", True)):
        return None
    model_candidates = _planner_model_candidates(ws)
    if not model_candidates:
        return None
    total = max(1, int(getattr(ws, "omni_post_verify_react_max_steps", 6) or 6))
    ro_max = max(0, int(getattr(ws, "omni_post_verify_react_readonly_max", 4) or 4))
    ns = (namespace or "").strip()
    dep = (deployment or "").strip()
    if not ns or not dep:
        return None
    ro_cap = _planner_readonly_output_cap(ws)
    s = sanitize_probe_text_for_llm(verify_summary, dep)
    thought_process: list[str] = []
    observations: list[str] = []
    discovery_steps: list[str] = []
    base_user = (
        f"WORKLOAD_IDENTITY namespace={ns!r} deployment={dep!r} "
        "(stable; do not pivot remediation to an ephemeral Pod name).\n"
        f"DEPLOYMENT_API_STATE: {dep_detail}\n"
        f"SDK_PROBE_SUMMARY (pod tokens redacted): {s}\n"
        f"LAST_EXECUTOR_STDOUT (excerpt):\n{(executor_stdout or '')[:3500]}\n\n"
        "Stages: observe → verify (read-only tools) → remediate (mutate). "
        "Session memory carries prior thoughts and observations — refine CoT each round.\n"
        "WORKLOAD-FIRST POLICY: never target Pod directly for diagnosis/remediation in this loop. "
        "Use namespace+deployment identity and workload-scoped tools only.\n"
        f"{LLM_TOOL_CATALOG_SECURITY_MUTATE}"
        f"{LLM_TOOL_CATALOG_CONFIGMAP_MUTATE}"
        f"Mutate allowlist (full): {', '.join(sorted(MUTATE_TOOL_ALLOWLIST))}.\n"
        f"Read-only examples: {', '.join(sorted(list(READONLY_TOOL_ALLOWLIST))[:14])}...\n"
        "For k8s_describe_resource on the workload: resource_type=Deployment, name=<deployment>, namespace=<namespace>.\n"
        "For k8s_rollout_restart, namespace and deployment MUST match WORKLOAD_IDENTITY exactly.\n"
    )

    for step in range(total):
        mem_obj = {
            "thoughts": thought_process[-12:],
            "observation_excerpts": [o[:900] for o in observations[-8:]],
        }
        mem_block = "Session memory (short-term CoT + observations):\n" + json.dumps(mem_obj, ensure_ascii=False)[
            :8000
        ]
        obs_tail = ""
        if observations:
            obs_tail = "\nLatest raw observations (truncated):\n" + "\n---\n".join(observations[-3:]) + "\n"
        user_content = base_user + "\n" + mem_block + obs_tail + f"\n(round {step + 1}/{total})\n"
        messages = [
            {"role": "system", "content": _POST_VERIFY_REACT_SYSTEM},
            {"role": "user", "content": user_content},
        ]
        parsed_any: dict[str, Any] | None = None
        last_pv_content = ""
        for model in model_candidates:
            try:
                resp = await llm.chat(model=model, messages=messages, stream=False, format="json")
            except Exception as me:
                logger.warning("post_verify_react model=%s: %s", model, me)
                log_llm_trace(
                    ws,
                    trace=trace,
                    phase="post_verify_react_chat_error",
                    model=str(model),
                    step=step + 1,
                    detail=str(me)[:800],
                )
                continue
            msg = (resp or {}).get("message") or {}
            content = str(msg.get("content") or "")
            last_pv_content = content
            parsed_any = _parse_agentic_json(content)
            if parsed_any is None:
                log_llm_trace(
                    ws,
                    trace=trace,
                    phase="post_verify_react",
                    model=str(model),
                    step=step + 1,
                    raw_response=content,
                    parse_ok=False,
                    parse_hint=agentic_parse_failure_hint(content),
                )
            else:
                log_llm_trace(
                    ws,
                    trace=trace,
                    phase="post_verify_react",
                    model=str(model),
                    step=step + 1,
                    raw_response=content,
                    parse_ok=True,
                    parsed_tool=str(parsed_any.get("tool_name") or "").strip() or None,
                )
            if parsed_any:
                break
        if not parsed_any:
            log_llm_trace(
                ws,
                trace=trace,
                phase="post_verify_react_no_parse_all_models",
                step=step + 1,
                raw_response=last_pv_content,
                parse_ok=False,
                parse_hint=agentic_parse_failure_hint(last_pv_content),
            )
            await _persist_post_verify_memory(
                ctx, trace, thought_process=thought_process, observations=observations, step_idx=step + 1
            )
            continue

        ph = str(parsed_any.get("phase") or "").strip().lower()
        analysis_line = str(parsed_any.get("thought") or parsed_any.get("analysis") or "").strip()
        if analysis_line:
            thought_process.append(f"[r{step + 1}/{ph or 'step'}] {analysis_line}")

        if ph == "done":
            await _clear_post_verify_memory(ctx, trace)
            logger.info("event=post_verify_react_phase_done trace=%s step=%s", trace, step + 1)
            return None

        tn = str(parsed_any.get("tool_name") or "").strip()
        args_raw = parsed_any.get("args") or parsed_any.get("tool_args")
        args = args_raw if isinstance(args_raw, dict) else {}

        if _readonly_tool_router(tn):
            if len(discovery_steps) >= ro_max:
                thought_process.append("readonly_budget_exhausted")
                await _persist_post_verify_memory(
                    ctx, trace, thought_process=thought_process, observations=observations, step_idx=step + 1
                )
                continue
            if _is_pod_scoped_readonly_request(tn, args):
                redirected_tn, redirected_args, reason = _rewrite_pod_scoped_to_workload_probe(
                    tn,
                    args,
                    namespace=ns,
                    deployment=dep,
                )
                thought_process.append(reason)
                tn = redirected_tn
                args = redirected_args
            exec_args = coerce_k8s_readonly_args(tn, dict(args))
            obs = await _execute_readonly_tool(ctx, tn, exec_args, output_max_chars=ro_cap)
            discovery_steps.append(tn)
            observations.append(f"tool={tn}\n{obs}")
            logger.info(
                "event=post_verify_react_readonly trace=%s step=%s tool=%s",
                trace,
                step + 1,
                tn,
            )
            await _persist_post_verify_memory(
                ctx, trace, thought_process=thought_process, observations=observations, step_idx=step + 1
            )
            continue

        if not tn:
            await _persist_post_verify_memory(
                ctx, trace, thought_process=thought_process, observations=observations, step_idx=step + 1
            )
            continue

        if not discovery_steps and tn in MUTATE_TOOL_ALLOWLIST:
            thought_process.append("policy:require_readonly_discovery_before_mutate")
            await _persist_post_verify_memory(
                ctx, trace, thought_process=thought_process, observations=observations, step_idx=step + 1
            )
            continue

        reason = _reject_reason(parsed_any)
        if reason:
            thought_process.append(f"schema_reject:{reason}:{tn}")
            log_llm_trace(
                ws,
                trace=trace,
                phase="post_verify_react_reject",
                step=step + 1,
                parse_ok=True,
                parsed_tool=tn or None,
                reject_reason=reason,
                detail=json.dumps(parsed_any, ensure_ascii=False, default=str)[:2500],
            )
            await _persist_post_verify_memory(
                ctx, trace, thought_process=thought_process, observations=observations, step_idx=step + 1
            )
            continue
        if not _post_verify_namespace_bound_ok(parsed_any, bound_ns=ns, bound_dep=dep):
            thought_process.append("reject:namespace_or_deployment_bound")
            await _persist_post_verify_memory(
                ctx, trace, thought_process=thought_process, observations=observations, step_idx=step + 1
            )
            continue

        tp: list[str] = list(thought_process)
        rc = {
            "verdict": "POST_VERIFY_REACT",
            "lane": "state",
            "thought_process": tp
            + ([f"discovery_tools:{','.join(discovery_steps)}"] if discovery_steps else [])
            + [f"final_mutate:{tn}"],
        }
        out: dict[str, Any] = {
            "tool_name": tn,
            "args": dict(args),
            "reasoning_chain": rc,
            "discovery_steps": discovery_steps,
        }
        logger.info(
            "event=post_verify_react_plan trace=%s step=%s tool=%s",
            trace,
            step + 1,
            tn,
        )
        await _clear_post_verify_memory(ctx, trace)
        return out

    await _clear_post_verify_memory(ctx, trace)
    logger.info("event=post_verify_react_exhausted trace=%s", trace)
    return None


# Backward-compatible alias for diagnostic spec docs
def discovery_tool_registry_names_for_spec(spec_name: str) -> tuple[str, ...]:
    return DISCOVERY_TOOL_ALIASES.get(spec_name, ())
