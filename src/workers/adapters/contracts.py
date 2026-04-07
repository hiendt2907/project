"""Portable autonomy adapter interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class AdapterEvent:
    trace_id: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AdapterPlan:
    trace_id: str
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0


@dataclass(slots=True)
class AdapterExecutionResult:
    trace_id: str
    status: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""


@dataclass(slots=True)
class AdapterCapabilityPolicy:
    """Adapter-level governance contract for portability expansion."""

    adapter_name: str
    allowed_mutators: set[str] = field(default_factory=set)
    allowed_namespaces: set[str] = field(default_factory=set)
    require_approval_in_prod: bool = True


def policy_allows_execute(
    policy: AdapterCapabilityPolicy,
    *,
    env_mode: str,
    tool_name: str,
    namespace: str = "",
) -> tuple[bool, str]:
    """Adapter policy check shared by future non-K8s adapters."""
    tn = str(tool_name or "").strip()
    ns = str(namespace or "").strip()
    if tn not in set(policy.allowed_mutators):
        return False, "tool_not_allowed_by_adapter_policy"
    em = str(env_mode or "prod").strip().lower()
    if em == "prod" and policy.require_approval_in_prod:
        return False, "approval_required_in_prod"
    if policy.allowed_namespaces and ns and ns not in set(policy.allowed_namespaces):
        return False, "namespace_not_allowed_by_adapter_policy"
    return True, ""


class IngressAdapter(Protocol):
    async def ingest(self, raw: dict[str, Any]) -> AdapterEvent: ...


class ProbeAdapter(Protocol):
    async def diagnose(self, ev: AdapterEvent) -> dict[str, Any]: ...


class PlannerAdapter(Protocol):
    async def plan(self, ev: AdapterEvent, diagnosis: dict[str, Any]) -> AdapterPlan | None: ...


class ActuatorAdapter(Protocol):
    async def execute(self, plan: AdapterPlan) -> AdapterExecutionResult: ...


class VerifierAdapter(Protocol):
    async def verify(self, ev: AdapterEvent, result: AdapterExecutionResult) -> dict[str, Any]: ...
