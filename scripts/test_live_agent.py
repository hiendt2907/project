#!/usr/bin/env python3
"""
Live E2E chaos test: CrashLoopBackOff → ReAct + CoT loop via run_agentic_mutate_plan.

Usage (from repo root):
    PYTHONPATH=src python scripts/test_live_agent.py

Env overrides:
    OMNI_VLLM_BASE_URL  — default http://host.orb.internal:11434/v1
    OMNI_CHAT_MODEL     — default qwen3.6
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import types
from typing import Any

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test_live_agent")

# ── scenario ─────────────────────────────────────────────────────────────────
ALERT_TEXT = (
    "CRITICAL CrashLoopBackOff — namespace: production, pod: payment-gateway-v2\n"
    "Container logs: FATAL: password authentication failed for user db_admin\n"
    "Pod has restarted 14 times in the last 10 minutes. OOMKilled=false.\n"
    "Alert: KubePodCrashLooping | severity: critical"
)

FAKE_EVIDENCE_BATCH = [
    {
        "probe": "k8s_pod_state",
        "alert_rule": "KubePodCrashLooping",
        "alert_hint": "CrashLoopBackOff — 14 restarts",
        "extracted_fact": {
            "namespace": "production",
            "pod": "payment-gateway-v2",
            "reason": "CrashLoopBackOff",
            "restart_count": 14,
            "last_state": "terminated",
            "exit_code": 1,
        },
    },
    {
        "probe": "k8s_logs",
        "alert_rule": "KubePodCrashLooping",
        "alert_hint": "DB auth failure in container logs",
        "extracted_fact": {
            "log_excerpt": "FATAL: password authentication failed for user db_admin",
            "source": "payment-gateway-v2",
            "level": "FATAL",
        },
    },
    {
        "probe": "k8s_events",
        "alert_rule": "KubePodCrashLooping",
        "alert_hint": "BackOff restarting failed container",
        "extracted_fact": {
            "event_type": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "count": 28,
        },
    },
]


# ── minimal stub ctx ──────────────────────────────────────────────────────────
def _resolve_base_url() -> str:
    """
    host.orb.internal resolves only inside K8s pods.
    On the macOS host, use localhost:11434 instead.
    OMNI_VLLM_BASE_URL env always wins.
    """
    explicit = os.getenv("OMNI_VLLM_BASE_URL", "")
    if explicit:
        return explicit
    in_cluster = bool(os.getenv("KUBERNETES_SERVICE_HOST"))
    return "http://host.orb.internal:11434/v1" if in_cluster else "http://localhost:11434/v1"


def _build_ctx() -> types.SimpleNamespace:
    """Build a minimal context that satisfies run_agentic_mutate_plan."""
    from llm.vllm_client import VLLMClient
    from workers.settings import WorkerSettings

    ws = WorkerSettings()

    base_url = _resolve_base_url()
    chat_model = os.getenv("OMNI_CHAT_MODEL", ws.chat_model)

    log.info("LLM base_url  : %s", base_url)
    log.info("chat model    : %s", chat_model)

    ws.vllm_base_url = base_url
    ws.chat_model = chat_model
    ws.model_reasoning_engine = chat_model
    ws.model_heavy_lifter = chat_model
    ws.model_helper = chat_model
    ws.diag_evidence_llm_model = chat_model
    # Force ReAct on so we see multi-step tool calls.
    ws.omni_diagnostic_react_enabled = True
    ws.omni_diagnostic_react_readonly_max = 2

    llm = VLLMClient(base_url=base_url, embed_url=base_url)

    llm = _wrap_llm(llm)

    ctx = types.SimpleNamespace(
        settings=ws,
        llm=llm,
        redis=None,
        kafka=None,
        semaphore=_FakeSemaphore(),
        llm_slot_held=False,
    )
    return ctx


class _FakeSemaphore:
    """No-op semaphore so the loop doesn't block."""
    async def acquire(self) -> object:
        return _FakeToken()

    async def release(self, token: object) -> None:
        pass


class _FakeToken:
    pass


# ── intercept LLM + tool calls for visibility ─────────────────────────────────
_EXCHANGE_LOG: list[dict] = []


class _LoggingLLMProxy:
    """Proxy around VLLMClient that logs every chat exchange to stdout."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    async def chat(self, **kwargs):
        resp = await self._inner.chat(**kwargs)  # type: ignore[attr-defined]
        self._print_exchange(kwargs, resp)
        return resp

    async def chat_plain(self, **kwargs):
        resp = await self._inner.chat_plain(**kwargs)  # type: ignore[attr-defined]
        self._print_exchange(kwargs, resp)
        return resp

    async def chat_structured(self, **kwargs):
        resp = await self._inner.chat_structured(**kwargs)  # type: ignore[attr-defined]
        self._print_exchange(kwargs, resp)
        return resp

    def _print_exchange(self, kwargs: dict, resp: dict) -> None:
        content = (resp.get("message") or {}).get("content", "")
        step = len(_EXCHANGE_LOG) + 1
        _EXCHANGE_LOG.append({"step": step, "model": kwargs.get("model"), "raw": content})
        width = 64
        print(f"\n{'─' * width}")
        print(f"  LLM Step {step}  model={kwargs.get('model')}")
        print(f"{'─' * width}")
        print(content[:1600])

    async def embed(self, **kwargs):
        return await self._inner.embed(**kwargs)  # type: ignore[attr-defined]

    async def aclose(self):
        return await self._inner.aclose()  # type: ignore[attr-defined]


def _wrap_llm(llm: object) -> object:
    return _LoggingLLMProxy(llm)


_FAKE_POD_DATA = (
    "[DATA] Pod: payment-gateway-v2  Namespace: production  Status: CrashLoopBackOff\n"
    "RestartCount: 14  Reason: Error  ExitCode: 1\n"
    "Env refs: DB_PASSWORD from Secret 'pg-credentials' key 'password'\n"
    "Events: BackOff restarting failed container (x28)\n"
    "[DIAGNOSIS] Pod is CrashLooping; envFrom secretRef=pg-credentials → auth fails. "
    "Verify Secret value matches current DB password."
)

_FAKE_SECRET_DATA = (
    "[DATA] Secret: pg-credentials  Namespace: production  Type: Opaque\n"
    "Keys: password (value REDACTED — base64 encoded)\n"
    "LastModified: 2025-12-01T00:00:00Z  (90 days ago)\n"
    "[DIAGNOSIS] Secret 'pg-credentials' exists but its 'password' key was last updated 90 days ago. "
    "The PostgreSQL password was rotated 3 days ago. "
    "Secret value is STALE — does not match current DB credentials. "
    "ACTION REQUIRED: update Secret 'pg-credentials' key 'password' with current DB password. "
    "NOTE: Omni cannot rotate Secrets autonomously. Human intervention required to update the Secret value. "
    "After Secret update, a rollout restart of payment-gateway-v2 will pick up the new value."
)

_FAKE_LOGS = (
    "[DATA] payment-gateway-v2 last 20 lines:\n"
    "FATAL: password authentication failed for user db_admin\n"
    "FATAL: password authentication failed for user db_admin\n"
    "Connection to postgres:5432 refused after 3 retries.\n"
    "[DIAGNOSIS] DB password mismatch confirmed. Secret rotation required — then restart."
)


async def _fake_tool_router(tool_name: str, args: dict, ctx: object) -> str:
    """Return resource-aware canned observations for readonly tool calls."""
    resource_type = str(args.get("resource_type") or args.get("kind") or "").lower()
    name = str(args.get("name") or args.get("pod_name") or "")

    if tool_name in ("k8s_describe_resource", "k8s_get_resource"):
        if "secret" in resource_type:
            return _FAKE_SECRET_DATA
        if "pod" in resource_type or "payment" in name:
            return _FAKE_POD_DATA
        return _FAKE_POD_DATA

    if tool_name in ("k8s_tail_logs", "get_pod_logs"):
        return _FAKE_LOGS

    if tool_name in ("inspect_pod_deep",):
        return _FAKE_POD_DATA

    if tool_name in ("k8s_list_pods", "list_all_pods_sdk"):
        return (
            "[DATA] production namespace pods:\n"
            "NAME                        READY  STATUS             RESTARTS\n"
            "payment-gateway-v2-xxx      0/1    CrashLoopBackOff   14\n"
            "[DIAGNOSIS] Only one pod affected; not cluster-wide."
        )

    return (
        f"[DATA] tool={tool_name} resource_type={resource_type} name={name}\n"
        "[DIAGNOSIS] Resource found; no additional anomaly detected."
    )


# ── helpers ───────────────────────────────────────────────────────────────────
def _section(title: str) -> None:
    width = 72
    print(f"\n{'═' * width}")
    print(f"  {title}")
    print(f"{'═' * width}")


def _pretty(label: str, obj: object) -> None:
    print(f"\n── {label} ──")
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    else:
        print(str(obj))


# ── step 1: connection ping ───────────────────────────────────────────────────
async def _ping_ollama(base_url: str) -> None:
    import httpx

    _section("STEP 1 — Connection Verification")
    url = base_url.rstrip("/").removesuffix("/v1") + "/v1/models"
    log.info("GET %s", url)
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.get(url)
        r.raise_for_status()
    models = r.json()
    ids = [m["id"] for m in models.get("data", [])]
    print(f"  Reachable. Models available: {ids}")
    for required in ("qwen3.6", "nomic-embed-text:latest"):
        tag = "✓" if any(required.split(":")[0] in i for i in ids) else "✗ MISSING"
        print(f"  {tag}  {required}")


# ── step 2: raw chat sanity — json_object enforced ───────────────────────────
async def _raw_json_sanity(llm: object, model: str) -> None:
    _section("STEP 2 — Raw JSON Sanity (response_format=json_object)")
    resp = await llm.chat(  # type: ignore[attr-defined]
        model=model,
        messages=[
            {"role": "system", "content": "You are a JSON-only assistant. Reply with a JSON object only."},
            {"role": "user", "content": 'Return: {"status": "ok", "model": "<your model name>"}'},
        ],
        format="json",
    )
    raw = (resp.get("message") or {}).get("content", "")
    print(f"  Raw response : {raw[:300]}")
    parsed = json.loads(raw)
    print(f"  Parsed OK    : {parsed}")


# ── step 3: ReAct mutate plan ────────────────────────────────────────────────
async def _run_react_plan(ctx: types.SimpleNamespace) -> dict:
    import workers.analyst_agentic_loop as aal

    # Patch the readonly tool executor with fake K8s observations so the
    # model can reason through to a conclusion without a live cluster.
    original_execute = aal._execute_readonly_tool

    async def _stubbed_execute(ctx_inner, tool_name: str, args: dict, **kwargs: Any) -> str:
        result = await _fake_tool_router(tool_name, args, ctx_inner)
        print(f"\n  [TOOL STUB] {tool_name}({json.dumps(args)[:120]})")
        print(f"  → {result[:300]}")
        return result

    aal._execute_readonly_tool = _stubbed_execute

    _section("STEP 3 — ReAct + CoT Loop  (run_agentic_mutate_plan)")
    print(f"\nScenario:\n  {ALERT_TEXT}\n")
    log.info("Starting agentic plan …")

    try:
        plan = await aal.run_agentic_mutate_plan(
            ctx,
            trace="chaos-test-001",
            sanitized_text=ALERT_TEXT,
            batch=FAKE_EVIDENCE_BATCH,
            max_steps=6,
        )
    finally:
        aal._execute_readonly_tool = original_execute

    return plan or {}


# ── main ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    ctx = _build_ctx()
    base_url: str = ctx.settings.vllm_base_url
    model: str = ctx.settings.chat_model

    await _ping_ollama(base_url)
    await _raw_json_sanity(ctx.llm, model)
    plan = await _run_react_plan(ctx)

    _section("RESULTS")

    reasoning = plan.get("reasoning_chain") or {}
    thought_process = reasoning.get("thought_process") or plan.get("thought_process") or []

    _pretty("Reasoning Chain (thought_process)", thought_process)
    _pretty("Final Remediation Plan (JSON)", {
        "tool_name":       plan.get("tool_name", ""),
        "args":            plan.get("args", {}),
        "reason_code":     plan.get("reason_code", ""),
        "phase":           plan.get("phase", ""),
        "final_analysis":  plan.get("final_analysis", ""),
        "discovery_steps": plan.get("discovery_steps", []),
        "lane_hint":       plan.get("lane_hint", ""),
        "reasoning_chain": reasoning,
    })

    tool = plan.get("tool_name", "")
    if tool:
        print(f"\n  ✓  Agent proposed tool: {tool}")
    elif plan.get("reason_code") == "PLANNER_PHASE_DONE":
        print("\n  ✓  Agent reached phase=done (no automated mutate; see final_analysis)")
    else:
        print("\n  ✗  No plan returned — check model / connectivity")

    print()


if __name__ == "__main__":
    asyncio.run(main())
