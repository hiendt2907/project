"""
Probe-structured_hint → EXECUTE_MUTATE without LLM when evidence is complete.

Design:
- Probes set ``extracted_fact.status=FAILED`` and ``recommended_tool`` (+ optional ``mutate_args``).
- Tool allowlist is intersected with ``MUTATE_TOOL_ALLOWLIST`` and optional CSV settings/env.
- Namespace / binding / ConfigMap names prefer structured_hint, then env mirrors, then CSV first namespace.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from pkg.reasoning.incident_matrix_profile import alertname_from_batch
from workers.autonomous_execute import MUTATE_TOOL_ALLOWLIST
from workers.evidence_mutate_emit import rollout_args_from_evidence_batch, workload_fault_incident_rollout_eligible


_RE_BROKEN_SPEC = re.compile(
    r"(createcontainerconfigerror|createcontainererror|failedmount|"
    r"configmap.*not\s+found|secret.*not\s+found|references\s+non-existent|"
    r"no\s+such\s+configmap|could\s+not\s+find\s+configmap)",
    re.IGNORECASE,
)

# Credential failure: DB/API auth errors indicate a stale credential in a Secret.
# Rollout restart CANNOT fix these — the LLM must call k8s_patch_secret.
_RE_CREDENTIAL_FAILURE = re.compile(
    r"(password\s+authentication\s+failed|authentication\s+failed\s+for\s+user|"
    r"access\s+denied\s+for\s+user|invalid\s+password|"
    r"authentication\s+error|credential.*invalid|invalid.*credential|"
    r"FATAL.*password|psql.*error.*password)",
    re.IGNORECASE,
)


def _evidence_suggests_credential_failure(batch: list[dict[str, Any]]) -> bool:
    """True when pod logs show DB/API auth failure → rollout restart won't help."""
    for b in batch:
        for field in ("raw", "alert_hint", "result"):
            val = str(b.get(field) or "")
            if _RE_CREDENTIAL_FAILURE.search(val):
                return True
        ef = b.get("extracted_fact")
        if isinstance(ef, dict):
            blob = json.dumps(ef, ensure_ascii=False)
        elif isinstance(ef, str):
            blob = ef
        else:
            blob = ""
        if blob and _RE_CREDENTIAL_FAILURE.search(blob):
            return True
    return False


def _evidence_suggests_broken_spec(batch: list[dict[str, Any]]) -> bool:
    blob_parts: list[str] = []
    for b in batch:
        blob_parts.append(str(b.get("alert_hint") or ""))
        blob_parts.append(str(b.get("raw") or ""))
        ef = b.get("extracted_fact")
        if isinstance(ef, dict):
            blob_parts.append(json.dumps(ef, ensure_ascii=False))
        elif isinstance(ef, str):
            blob_parts.append(ef)
        blob_parts.append(str(b.get("canonical_query_snippet") or ""))
    return bool(_RE_BROKEN_SPEC.search("\n".join(blob_parts)))


def _normalize_extracted_fact(raw: Any) -> dict[str, Any] | None:
    """``coerce_evidence_dict`` may JSON-serialize dicts — accept str or dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            o = json.loads(raw)
            return o if isinstance(o, dict) else None
        except Exception:
            return None
    return None


def env_default_remediation_namespace() -> str:
    """First entry of OMNI_AUTONOMOUS_ALLOWED_NAMESPACES (mirrors WorkerSettings default)."""
    raw = os.environ.get("OMNI_AUTONOMOUS_ALLOWED_NAMESPACES", "multi-agent").strip()
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    return parts[0] if parts else "multi-agent"


def parse_probe_driven_mutate_tools_csv(raw: str | None) -> frozenset[str]:
    """Parse CSV of tool names; empty → built-in security-oriented defaults."""
    if raw is None:
        raw = ""
    s = str(raw).strip()
    if not s:
        base = {"k8s_apply_rbac_least_privilege", "k8s_patch_configmap"}
    else:
        base = {x.strip() for x in s.replace(";", ",").split(",") if x.strip()}
    return frozenset(base) & MUTATE_TOOL_ALLOWLIST


def probe_driven_mutate_tools_for_settings(ws: Any | None) -> frozenset[str]:
    """CSV from settings ``omni_probe_driven_mutate_tools`` or env ``OMNI_PROBE_DRIVEN_MUTATE_TOOLS``."""
    if ws is not None:
        csv = str(getattr(ws, "omni_probe_driven_mutate_tools", "") or "").strip()
        if csv:
            return parse_probe_driven_mutate_tools_csv(csv)
    env_csv = os.environ.get("OMNI_PROBE_DRIVEN_MUTATE_TOOLS", "").strip()
    if env_csv:
        return parse_probe_driven_mutate_tools_csv(env_csv)
    return parse_probe_driven_mutate_tools_csv("")


def default_remediation_namespace(ws: Any | None) -> str:
    if ws is not None:
        from workers.env_mode import parse_allowed_namespaces

        allowed = parse_allowed_namespaces(ws)
        if allowed:
            return next(iter(sorted(allowed)))
    return env_default_remediation_namespace()


def _rbac_defaults() -> dict[str, str]:
    return {
        "executor_sa": os.environ.get("OMNI_EXECUTOR_SA", "omni-worker").strip() or "omni-worker",
        "remove_cluster_admin_binding": (
            os.environ.get("OMNI_CLUSTER_ADMIN_BINDING", "omni-worker-cluster-admin").strip()
            or "omni-worker-cluster-admin"
        ),
    }


def _configmap_defaults() -> dict[str, str]:
    return {
        "name": os.environ.get("OMNI_WORKER_CONFIGMAP_NAME", "omni-worker-config").strip() or "omni-worker-config",
        "key": os.environ.get("OMNI_GOD_MODE_PATCH_KEY", "OMNI_GOD_MODE").strip() or "OMNI_GOD_MODE",
        "value": os.environ.get("OMNI_GOD_MODE_PATCH_VALUE", "false").strip() or "false",
    }


def _build_tool_args(
    tool: str,
    ef: dict[str, Any],
    *,
    default_ns: str,
) -> dict[str, Any] | None:
    """Map structured_hint / mutate_args into registry kwargs; None if incomplete."""
    ma = ef.get("mutate_args")
    if isinstance(ma, dict) and ma:
        merged = dict(ma)
        merged.setdefault("namespace", ef.get("namespace") or default_ns)
        if tool == "k8s_patch_configmap":
            merged.setdefault("name", ef.get("configmap_name") or ef.get("name"))
            merged.setdefault("key", ef.get("patch_key") or ef.get("key"))
            if "value" not in merged and "patch_value" in ef:
                merged["value"] = ef.get("patch_value")
        if tool == "k8s_apply_rbac_least_privilege":
            merged.setdefault("executor_sa", ef.get("executor_sa"))
            merged.setdefault("remove_cluster_admin_binding", ef.get("remove_cluster_admin_binding"))
        merged.setdefault("reasoning", ef.get("reasoning") or "probe_mutate_args")
        return _validate_tool_args(tool, merged, default_ns=default_ns)

    if tool == "k8s_apply_rbac_least_privilege":
        d = _rbac_defaults()
        ns = str(ef.get("namespace") or default_ns).strip()
        if not ns:
            return None
        ex = str(ef.get("executor_sa") or d["executor_sa"]).strip()
        rm = str(ef.get("remove_cluster_admin_binding") or ef.get("binding_name") or d["remove_cluster_admin_binding"]).strip()
        rs = str(ef.get("reasoning") or "").strip() or "probe_structured_hint"
        return _validate_tool_args(
            tool,
            {"namespace": ns, "executor_sa": ex, "remove_cluster_admin_binding": rm, "reasoning": rs},
            default_ns=default_ns,
        )

    if tool == "k8s_patch_configmap":
        d = _configmap_defaults()
        name = str(ef.get("configmap_name") or ef.get("name") or d["name"]).strip()
        ns = str(ef.get("namespace") or default_ns).strip()
        key = str(ef.get("patch_key") or ef.get("key") or d["key"]).strip()
        val = ef.get("patch_value")
        if val is None:
            val = ef.get("value")
        if val is None:
            val = d["value"]
        val_s = str(val) if val is not None else ""
        rs = str(ef.get("reasoning") or "").strip() or "probe_structured_hint"
        if not name or not ns or not key:
            return None
        return _validate_tool_args(
            tool,
            {"name": name, "namespace": ns, "key": key, "value": val_s, "reasoning": rs},
            default_ns=default_ns,
        )

    return None


def _validate_tool_args(
    tool: str,
    args: dict[str, Any],
    *,
    default_ns: str,
) -> dict[str, Any] | None:
    """Minimal Pydantic-shaped validation for executor."""
    if tool == "k8s_apply_rbac_least_privilege":
        ns = str(args.get("namespace") or default_ns).strip()
        if not ns:
            return None
        return {
            "namespace": ns,
            "executor_sa": str(args.get("executor_sa") or "omni-worker").strip(),
            "remove_cluster_admin_binding": str(args.get("remove_cluster_admin_binding") or "").strip(),
            "reasoning": str(args.get("reasoning") or "")[:500],
        }
    if tool == "k8s_patch_configmap":
        name = str(args.get("name") or "").strip()
        ns = str(args.get("namespace") or default_ns).strip()
        key = str(args.get("key") or "").strip()
        if not name or not ns or not key:
            return None
        return {
            "name": name,
            "namespace": ns,
            "key": key,
            "value": str(args.get("value") if args.get("value") is not None else ""),
            "reasoning": str(args.get("reasoning") or "")[:500],
        }
    if tool == "k8s_patch_secret":
        name = str(args.get("name") or "").strip()
        ns = str(args.get("namespace") or default_ns).strip()
        key = str(args.get("key") or "").strip()
        if not name or not ns or not key:
            return None
        val = args.get("value")
        if val is None:
            return None
        out: dict[str, Any] = {
            "name": name,
            "namespace": ns,
            "key": key,
            "value": str(val),
            "reasoning": str(args.get("reasoning") or "")[:500],
        }
        vs = str(args.get("value_source") or "").strip()
        vr = str(args.get("value_source_ref") or "").strip()
        if vs:
            out["value_source"] = vs
        if vr:
            out["value_source_ref"] = vr
        return out
    return None


def probe_structured_remediation_ready(
    batch: list[dict[str, Any]],
    *,
    default_ns: str,
    allowed_tools: frozenset[str],
) -> bool:
    """True when any batch item can produce a deterministic mutate plan."""
    for b in batch:
        if deterministic_mutate_plan_from_item(b, default_ns=default_ns, allowed_tools=allowed_tools):
            return True
    return False


def deterministic_mutate_plan_from_item(
    item: dict[str, Any],
    *,
    default_ns: str,
    allowed_tools: frozenset[str],
) -> dict[str, Any] | None:
    """Single evidence item → plan dict or None."""
    ef = _normalize_extracted_fact(item.get("extracted_fact"))
    if not ef:
        return None
    if str(ef.get("status") or "").upper() != "FAILED":
        return None
    tool = str(ef.get("recommended_tool") or "").strip()
    if not tool or tool not in allowed_tools or tool not in MUTATE_TOOL_ALLOWLIST:
        return None
    args = _build_tool_args(tool, ef, default_ns=default_ns)
    if not args:
        return None
    pr = str(item.get("probe") or "").strip() or "unknown"
    rc: dict[str, Any] = {
        "verdict": "PROBE_REMEDIATE",
        "lane": "state",
        "thought_process": [f"structured_hint:{pr}:FAILED:{tool}"],
    }
    return {
        "tool_name": tool,
        "args": args,
        "discovery_steps": [],
        "reasoning_chain": rc,
    }


def _oom_deterministic_enabled(ws: Any | None) -> bool:
    if ws is not None:
        return bool(getattr(ws, "omni_oom_deterministic_remediate_enabled", False))
    raw = os.environ.get("OMNI_OOM_DETERMINISTIC_REMEDIATE_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes")


_NS_DEP_FROM_LABELS_JSON = re.compile(
    r'"namespace"\s*:\s*"([^"\\]*)".*?"deployment"\s*:\s*"([^"\\]*)"',
    re.DOTALL,
)
_NS_DEP_FROM_LABELS_JSON_ALT = re.compile(
    r'"deployment"\s*:\s*"([^"\\]*)".*?"namespace"\s*:\s*"([^"\\]*)"',
    re.DOTALL,
)


def _namespace_deployment_from_batch(batch: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Resolve ns+deployment from canonical_query JSON; tolerate truncated snippets (evidence [:N])."""
    for b in batch:
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if not snip.startswith("{"):
            continue
        try:
            j = json.loads(snip)
            labels = j.get("labels") if isinstance(j, dict) else None
            if isinstance(labels, dict):
                ns = str(labels.get("namespace") or "").strip()
                dep = str(labels.get("deployment") or "").strip()
                if ns and dep:
                    return ns, dep
        except Exception:
            pass
        m = _NS_DEP_FROM_LABELS_JSON.search(snip)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        m2 = _NS_DEP_FROM_LABELS_JSON_ALT.search(snip)
        if m2:
            return m2.group(2).strip(), m2.group(1).strip()
    return None


def _oom_patch_json(container: str, memory: str) -> str:
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": container, "resources": {"limits": {"memory": memory}}},
                    ]
                }
            }
        }
    }
    return json.dumps(patch, ensure_ascii=False)


def _batch_blob_lower(batch: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for b in batch:
        parts.append(str(b.get("raw") or ""))
        parts.append(str(b.get("alert_hint") or ""))
        parts.append(str(b.get("canonical_query_snippet") or ""))
        ef = b.get("extracted_fact")
        if isinstance(ef, dict):
            parts.append(json.dumps(ef, ensure_ascii=False))
        elif isinstance(ef, str):
            parts.append(ef)
    return "\n".join(parts).lower()


def chaos_credential_lab_autofix_plan_from_batch(
    batch: list[dict[str, Any]],
    *,
    default_ns: str,
    allowed_tools: frozenset[str],
    ws: Any | None = None,
) -> dict[str, Any] | None:
    """
    Lab-only: DB/API credential failure on chaos victim / chaos-pg → restore APP_PASSWORD via
    k8s_patch_secret using ``chaos_pg_app_password`` from settings (never set in prod).
    """
    if ws is None or not bool(getattr(ws, "lab_chaos_credential_autofix_enabled", False)):
        return None
    if "k8s_patch_secret" not in allowed_tools:
        return None
    if not _evidence_suggests_credential_failure(batch):
        return None
    pwd = str(getattr(ws, "chaos_pg_app_password", "") or "").strip()
    if not pwd:
        pwd = os.environ.get("OMNI_CHAOS_PG_APP_PASSWORD", "").strip()
    if not pwd:
        return None
    blob = _batch_blob_lower(batch)
    if not any(
        x in blob
        for x in (
            "chaos-victim",
            "chaos_app",
            "chaos-pg",
            "chaos_pg",
        )
    ):
        return None
    ns = str(getattr(ws, "chaos_lab_namespace", "") or "").strip()
    rr = rollout_args_from_evidence_batch(batch)
    if rr and str(rr.get("namespace") or "").strip():
        ns = str(rr["namespace"]).strip()
    if not ns:
        ns = default_ns
    secret_name = str(getattr(ws, "chaos_pg_secret_name", "chaos-pg-secret") or "").strip()
    key = str(getattr(ws, "chaos_pg_password_key", "APP_PASSWORD") or "").strip()
    if not secret_name or not key:
        return None
    args = _validate_tool_args(
        "k8s_patch_secret",
        {
            "namespace": ns,
            "name": secret_name,
            "key": key,
            "value": pwd,
            "value_source": "lab_chaos_autofix",
            "value_source_ref": "OMNI_CHAOS_PG_APP_PASSWORD",
            "reasoning": (
                "Lab chaos_credential_autofix: restore Secret key after password authentication "
                "failure in logs (OMNI_LAB_CHAOS_CREDENTIAL_AUTOFIX + OMNI_CHAOS_PG_APP_PASSWORD)."
            ),
        },
        default_ns=default_ns,
    )
    if not args:
        return None
    rc: dict[str, Any] = {
        "verdict": "CHAOS_CREDENTIAL_LAB_AUTOFIX",
        "lane": "state",
        "thought_process": [f"chaos_credential_lab_autofix:{ns}/{secret_name}:{key}"],
    }
    return {
        "tool_name": "k8s_patch_secret",
        "args": args,
        "discovery_steps": [],
        "reasoning_chain": rc,
    }


def fault_rollout_deterministic_plan_from_batch(
    batch: list[dict[str, Any]],
    *,
    ws: Any | None = None,
) -> dict[str, Any] | None:
    """
    Namespace + deployment from alert labels + workload fault signal → k8s_rollout_restart
    without LLM. Not gated on omni_probe_driven_mutate_tools CSV (rollout is policy-driven).
    """
    if ws is not None and not bool(getattr(ws, "omni_autonomous_rollout_on_fault_incident", True)):
        return None
    if "k8s_rollout_restart" not in MUTATE_TOOL_ALLOWLIST:
        return None
    rr = rollout_args_from_evidence_batch(batch)
    if not rr:
        return None
    if not workload_fault_incident_rollout_eligible(batch):
        return None
    # Hard-fail / broken-spec incidents must be fixed at source (ConfigMap/Secret),
    # not via rollout restart.
    if _evidence_suggests_broken_spec(batch):
        return None
    # Credential failures (DB auth, API auth) require k8s_patch_secret — rollout restart
    # cannot fix a stale password in a Secret. Defer to LLM agentic path.
    if _evidence_suggests_credential_failure(batch):
        return None
    ns = str(rr.get("namespace") or "").strip()
    dep = str(rr.get("deployment") or "").strip()
    if not ns or not dep:
        return None
    rc: dict[str, Any] = {
        "verdict": "FAULT_ROLLOUT_DETERMINISTIC",
        "lane": "state",
        "thought_process": [f"fault_rollout_deterministic:{ns}/{dep}"],
    }
    return {
        "tool_name": "k8s_rollout_restart",
        "args": {
            "namespace": ns,
            "deployment": dep,
            "reasoning": (
                "Deterministic rollout: workload fault incident with namespace+deployment in alert labels."
            ),
        },
        "discovery_steps": [],
        "reasoning_chain": rc,
    }


def oom_deterministic_plan_from_batch(
    batch: list[dict[str, Any]],
    *,
    default_ns: str,
    allowed_tools: frozenset[str],
    ws: Any | None = None,
) -> dict[str, Any] | None:
    """Lab-only: bump Deployment memory when alert is OOM and labels carry ns+deployment."""
    if not _oom_deterministic_enabled(ws):
        return None
    if "k8s_patch_resource" not in allowed_tools:
        return None
    if alertname_from_batch(batch) != "OmniOomKilledPodNoRecovery":
        return None
    nd = _namespace_deployment_from_batch(batch)
    if not nd:
        return None
    ns, dep = nd
    # Lab nginx-load workload uses container name "load" (not "nginx"); override via OMNI_OOM_PATCH_CONTAINER.
    container = os.environ.get("OMNI_OOM_PATCH_CONTAINER", "").strip() or "load"
    memory = os.environ.get("OMNI_OOM_PATCH_MEMORY", "512Mi").strip() or "512Mi"
    args = {
        "resource_type": "Deployment",
        "name": dep,
        "namespace": ns,
        "patch_json": _oom_patch_json(container, memory),
    }
    rc: dict[str, Any] = {
        "verdict": "PROBE_REMEDIATE",
        "lane": "state",
        "thought_process": [f"oom_deterministic:{ns}/{dep}:memory_bump"],
    }
    return {
        "tool_name": "k8s_patch_resource",
        "args": args,
        "discovery_steps": [],
        "reasoning_chain": rc,
    }


def deterministic_mutate_plan_from_batch(
    batch: list[dict[str, Any]],
    *,
    default_ns: str,
    allowed_tools: frozenset[str],
    ws: Any | None = None,
) -> dict[str, Any] | None:
    """First matching item wins; optional OOM lab path when settings enable it."""
    tools = allowed_tools
    if ws is not None and bool(getattr(ws, "lab_chaos_credential_autofix_enabled", False)):
        # CSV probe-driven allowlist often omits k8s_patch_secret; chaos lab autofix requires it.
        tools = frozenset(allowed_tools) | frozenset({"k8s_patch_secret"})
    for b in batch:
        plan = deterministic_mutate_plan_from_item(b, default_ns=default_ns, allowed_tools=tools)
        if plan:
            return plan
    cc = chaos_credential_lab_autofix_plan_from_batch(
        batch, default_ns=default_ns, allowed_tools=tools, ws=ws
    )
    if cc:
        return cc
    fr = fault_rollout_deterministic_plan_from_batch(batch, ws=ws)
    if fr:
        return fr
    if _oom_deterministic_enabled(ws):
        ext_tools = allowed_tools | frozenset({"k8s_patch_resource"})
        oom = oom_deterministic_plan_from_batch(
            batch, default_ns=default_ns, allowed_tools=ext_tools, ws=ws
        )
        if oom:
            return oom
    return None
