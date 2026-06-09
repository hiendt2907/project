"""Read-only verification runner — actually executes advisory verification_steps.

Part of the "full diagnosis loop": instead of merely citing documentation, we run
the advisory's read-only ``verification_steps`` to empirically test the hypothesis.

Hard invariants honoured here:
  * Strict read-only allowlist — any mutating / shell-escaping command is BLOCKED.
  * No ``subprocess`` for K8s. Execution is delegated to the project's existing
    async tool registry (``workers.tools.TOOL_REGISTRY``), which wraps
    kubernetes-asyncio / httpx PromQL. If no safe executor can be matched, the
    probe is reported as *degraded* rather than spawning anything new.
  * Best-effort: a per-probe failure never propagates; the entry function never
    raises.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_MAX_OUTPUT_CHARS = 2000
_ERROR_TRUNCATE_CHARS = 300

# Multi-word read-only command prefixes. A command is allowed only when its
# normalized text starts with one of these (after whitespace collapsing).
_READONLY_PREFIXES: tuple[str, ...] = (
    "kubectl get",
    "kubectl describe",
    "kubectl logs",
    "kubectl top",
    "kubectl version",
    "kubectl api-resources",
    "df",
    "free",
    "uptime",
    "top -b",
    "systemctl status",
    "systemctl show",
    "journalctl",
    "ss",
    "ip route",
    "ip addr",
    "dig",
    "nslookup",
)

# Tokens that indicate mutation, shell chaining, command substitution, or
# redirection. Their presence forces a BLOCK regardless of any prefix match.
_DANGER_PATTERN = re.compile(
    r"(?:\bdelete\b|\bapply\b|\bedit\b|\bscale\b|\brestart\b|\bset\b|\bpatch\b"
    r"|\brollout\b|\bexec\b|\bcordon\b|\bdrain\b|\brm\s|>|;|&&|`|\$\()",
    re.IGNORECASE,
)

_LAYER_PROMETHEUS = "prometheus"

# PromQL fragments used to recognise a metrics query when layer == prometheus.
_PROMQL_HINTS: tuple[str, ...] = (
    "rate(",
    "irate(",
    "increase(",
    "predict_linear(",
    "avg_over_time(",
    "sum(",
    "avg(",
    "max(",
    "min(",
    "count(",
    "histogram_quantile(",
    "node_",
    "kube_",
    "container_",
    "up{",
    "up ",
)


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of attempting one read-only verification step."""

    command: str
    layer: str
    ran: bool
    blocked: bool
    rc: int
    output: str
    error: str


def _normalize(cmd: str) -> str:
    """Collapse whitespace for stable prefix matching."""
    return " ".join((cmd or "").strip().split())


def is_readonly_command(cmd: str) -> bool:
    """Return True only when ``cmd`` is a strictly read-only diagnostic command.

    A command must (a) contain no mutation / shell-escape token and (b) start
    with a known read-only prefix.
    """
    norm = _normalize(cmd)
    if not norm:
        return False
    if _DANGER_PATTERN.search(norm):
        return False
    return any(
        norm == prefix or norm.startswith(prefix + " ")
        for prefix in _READONLY_PREFIXES
    )


def _looks_like_promql(cmd: str) -> bool:
    """Heuristic: does this string look like a PromQL expression?"""
    if _DANGER_PATTERN.search(cmd or ""):
        return False
    return any(hint in (cmd or "") for hint in _PROMQL_HINTS)


def _blocked(command: str, layer: str) -> ProbeResult:
    return ProbeResult(
        command=command,
        layer=layer,
        ran=False,
        blocked=True,
        rc=-1,
        output="",
        error="not in read-only allowlist",
    )


def _degraded(command: str, layer: str) -> ProbeResult:
    return ProbeResult(
        command=command,
        layer=layer,
        ran=False,
        blocked=False,
        rc=0,
        output="",
        error="no read-only executor available",
    )


def _map_command_to_tool(command: str, layer: str) -> tuple[str, dict[str, Any]] | None:
    """Map an allowlisted command to a registered read-only tool + args.

    Returns ``(tool_name, args)`` or ``None`` when no safe executor matches.
    Only kubectl read verbs and PromQL are mapped; host shell commands
    (df/free/top/...) have no in-process async executor and stay degraded.
    """
    norm = _normalize(command)

    # PromQL (layer-driven or shape-driven).
    if layer == _LAYER_PROMETHEUS or _looks_like_promql(norm):
        if norm:
            return "promql_instant", {"query": norm}
        return None

    # kubectl read verbs → typed read-only SDK tools.
    try:
        tokens = shlex.split(norm)
    except ValueError:
        tokens = norm.split()
    if len(tokens) >= 2 and tokens[0] == "kubectl":
        verb = tokens[1]
        if verb in {"get", "describe", "logs", "top"}:
            resource = tokens[2] if len(tokens) >= 3 else ""
            ns = _extract_namespace(tokens)
            if resource in {"pods", "pod", "po"}:
                args: dict[str, Any] = {"namespace": ns} if ns else {}
                return "list_namespace_pods", args
            if resource in {"nodes", "node", "no"}:
                return "k8s_list_nodes", {}
            if resource in {"svc", "service", "services"}:
                return "k8s_list_services", ({"namespace": ns} if ns else {})
            if resource in {"ingress", "ing"}:
                return "k8s_list_ingress", ({"namespace": ns} if ns else {})
    return None


def _extract_namespace(tokens: list[str]) -> str:
    """Pull a namespace out of ``-n ns`` / ``--namespace=ns`` style flags."""
    for i, tok in enumerate(tokens):
        if tok in {"-n", "--namespace"} and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--namespace="):
            return tok.split("=", 1)[1]
        if tok.startswith("-n="):
            return tok.split("=", 1)[1]
    return ""


async def _invoke_tool(ctx: Any, tool_name: str, args: dict[str, Any]) -> str | None:
    """Invoke a registered read-only tool, preferring the typed registry.

    Returns the tool's string output, or ``None`` if no executor is registered.
    """
    # Typed registry (kubernetes-asyncio SDK read-only tools).
    try:
        from workers.tool_registry import get_tool_registry

        registry = get_tool_registry()
        if registry.has(tool_name):
            return await registry.invoke(ctx, tool_name, args)
    except Exception:  # pragma: no cover - defensive; fall through to legacy map
        logger.debug("typed registry invoke failed for %s", tool_name, exc_info=True)

    # Legacy dict registry (PromQL via httpx, pod listing helpers).
    try:
        from workers.tools import TOOL_REGISTRY

        fn = TOOL_REGISTRY.get(tool_name)
        if fn is not None:
            return await fn(ctx, args)
    except Exception:  # pragma: no cover - defensive
        logger.debug("legacy registry invoke failed for %s", tool_name, exc_info=True)

    return None


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


async def _run_one(ctx: Any, command: str, layer: str) -> ProbeResult:
    """Validate + execute a single verification step. Never raises."""
    try:
        is_promql = layer == _LAYER_PROMETHEUS or _looks_like_promql(command)
        if not is_readonly_command(command) and not is_promql:
            return _blocked(command, layer)
        # PromQL must still be free of shell-escape tokens.
        if is_promql and _DANGER_PATTERN.search(command or ""):
            return _blocked(command, layer)

        mapping = _map_command_to_tool(command, layer)
        if mapping is None:
            return _degraded(command, layer)

        tool_name, args = mapping
        output = await _invoke_tool(ctx, tool_name, args)
        if output is None:
            return _degraded(command, layer)

        return ProbeResult(
            command=command,
            layer=layer,
            ran=True,
            blocked=False,
            rc=0,
            output=_truncate(str(output), _MAX_OUTPUT_CHARS),
            error="",
        )
    except Exception as exc:  # best-effort: isolate per-probe failures
        logger.warning("kb_verifier probe failed: %s", exc, exc_info=True)
        return ProbeResult(
            command=command,
            layer=layer,
            ran=False,
            blocked=False,
            rc=-1,
            output="",
            error=str(exc)[:_ERROR_TRUNCATE_CHARS],
        )


async def run_readonly_verification(
    ctx: Any,
    *,
    advisory: Any,
    trace: str,
    max_probes: int = 4,
) -> list[ProbeResult]:
    """Run the first ``max_probes`` read-only verification steps of an advisory.

    Each step's ``command`` is allowlist-checked; allowed commands are executed
    through the existing async tool registry (no subprocess). Mutating or
    shell-escaping commands are blocked; allowed-but-unmapped commands are
    reported as degraded. Returns one :class:`ProbeResult` per attempted step.
    """
    steps = list(getattr(advisory, "verification_steps", None) or [])[: max(0, max_probes)]
    results: list[ProbeResult] = []
    for step in steps:
        command = str(getattr(step, "command", "") or "")
        layer = str(getattr(step, "layer", "") or "")
        results.append(await _run_one(ctx, command, layer))

    logger.info(
        "event=kb_verification_done trace=%s probes=%s ran=%s blocked=%s degraded=%s",
        trace,
        len(results),
        sum(1 for r in results if r.ran),
        sum(1 for r in results if r.blocked),
        sum(1 for r in results if not r.ran and not r.blocked),
    )
    return results
