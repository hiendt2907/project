"""Normalization, redaction, and canonical query helpers for Omni observability pipeline.

Two responsibilities:
1. **Redaction** — strip PII/secrets from any string before it enters LLM prompt
   or RAG embed. Zero-tolerance: pattern match → [REDACTED_*] placeholder.
2. **Canonical Query** — produce a deterministic embed string from an anomaly event
   so that SOP vector search scores stay ≥ 0.9 when corpus taxonomy matches.
   Formula: ``[ACTION] [RESOURCE] [ERROR_SIGNATURE]``
"""

from __future__ import annotations

import re
from enum import Enum
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Redaction — PII & secrets
# ---------------------------------------------------------------------------

# Compiled once at import
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # JWT / Bearer tokens (long base64 blocks)
    (
        re.compile(
            r"\beyJh[A-Za-z0-9+/=_-]{20,}\.[A-Za-z0-9+/=_-]{10,}\.[A-Za-z0-9+/=_-]{10,}\b",
            re.ASCII,
        ),
        "[REDACTED_JWT]",
    ),
    # Bearer / token= value
    (
        re.compile(
            r"(?i)\b(bearer|token)\s*[=:]\s*['\"]?[A-Za-z0-9+/=_\-\.]{16,}['\"]?",
        ),
        r"\1=[REDACTED_TOKEN]",
    ),
    # password= / passwd= / pwd= / secret= — skip already-redacted placeholders
    (
        re.compile(
            r"(?i)(password|passwd|pwd|secret)\s*[=:]\s*['\"]?(?!\[REDACTED)[^\s,\])'\"]{4,}['\"]?",
        ),
        r"\1=[REDACTED_SECRET]",
    ),
    # Connection strings: postgres://user:pass@host, redis://:pass@..., mysql://...
    # Handles both full user:pass and empty-user :pass forms
    (
        re.compile(
            r"(?i)(postgres|postgresql|redis|mysql|mongodb|amqp)://[^@\s]*:[^@\s]+@",
        ),
        r"\1://[REDACTED_CREDS]@",
    ),
    # AWS Access Key IDs (AKIA...)
    (
        re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
        "[REDACTED_AWS_KEY_ID]",
    ),
    # AWS Secret Access Keys (40-char alphanumeric after common prefixes)
    (
        re.compile(
            r"(?i)(aws_secret_access_key|aws_secret)\s*[=:]\s*[A-Za-z0-9+/]{40}\b",
        ),
        r"\1=[REDACTED_AWS_SECRET]",
    ),
    # Generic private key blocks
    (
        re.compile(
            r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # IPv4 with /password pattern in kubeconfig or similar YAML
    (
        re.compile(
            r"(?i)(client[-_]?secret|api[-_]?key|apikey|auth[-_]?token)\s*[=:]\s*['\"]?[A-Za-z0-9+/_\-\.]{8,}['\"]?",
        ),
        r"\1=[REDACTED_API_KEY]",
    ),
]


def redact(text: str) -> str:
    """Return text with all secret/PII patterns replaced by safe placeholders.

    Safe to call multiple times (idempotent on already-redacted strings).
    Does NOT redact numeric metric values, pod names, or namespaces.
    """
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def is_clean(text: str) -> bool:
    """Return True if ``text`` contains no detectable secret patterns.

    Intended for unit-test assertions.
    """
    for pattern, _ in _REDACT_PATTERNS:
        if pattern.search(text):
            return False
    return True


# ---------------------------------------------------------------------------
# Canonical Query — deterministic embed string for SOP retrieval
# ---------------------------------------------------------------------------

class Action(str, Enum):
    """Verb taxonomy for the Canonical Query ACTION slot.

    Keep in sync with ``sop_mapping.md`` and SOP seed YAML titles.
    """
    CHECK = "CHECK"
    RESTART = "RESTART"
    SCALE = "SCALE"
    DRAIN = "DRAIN"
    DIAGNOSE = "DIAGNOSE"
    ROLLBACK = "ROLLBACK"
    PATCH = "PATCH"
    ALERT = "ALERT"


class Resource(str, Enum):
    """Resource taxonomy for the RESOURCE slot."""
    POD = "POD"
    DEPLOYMENT = "DEPLOYMENT"
    NODE = "NODE"
    SERVICE = "SERVICE"
    VOLUME = "VOLUME"
    REDIS = "REDIS"
    PGVECTOR = "PGVECTOR"
    PROMETHEUS = "PROMETHEUS"
    LOKI = "LOKI"
    INGRESS = "INGRESS"
    NAMESPACE = "NAMESPACE"
    CLUSTER = "CLUSTER"


class ErrorSignature(str, Enum):
    """Error/condition taxonomy for the ERROR_SIGNATURE slot."""
    OOM_KILLED = "OOM_KILLED"
    CPU_THROTTLE = "CPU_THROTTLE"
    CRASH_LOOP = "CRASH_LOOP"
    IMAGE_PULL = "IMAGE_PULL"
    PENDING = "PENDING"
    NOT_READY = "NOT_READY"
    HIGH_LOAD = "HIGH_LOAD"
    DISK_FULL = "DISK_FULL"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    DNS_FAIL = "DNS_FAIL"
    PROBE_FAIL = "PROBE_FAIL"
    REPLICA_MISMATCH = "REPLICA_MISMATCH"
    LATENCY_HIGH = "LATENCY_HIGH"
    CONNECTION_REFUSED = "CONNECTION_REFUSED"
    UNKNOWN = "UNKNOWN"


class CanonicalQuery(NamedTuple):
    """Structured representation of a canonical query triple."""
    action: str        # Action enum value or free-form fallback
    resource: str      # Resource enum value or free-form fallback
    error_sig: str     # ErrorSignature enum value or free-form fallback

    def to_embed_string(self, separator: str = " ") -> str:
        """Produce the deterministic embed string for RAG search.

        Default separator is space; use ``|`` if your corpus was ingested with
        that separator — keep ingest and query consistent.

        Example: ``"CHECK POD_REDIS OOM_KILLED"``
        """
        parts = [
            str(self.action).upper().strip(),
            str(self.resource).upper().strip(),
            str(self.error_sig).upper().strip(),
        ]
        return separator.join(p for p in parts if p)


def build_canonical_query(
    *,
    action: str | Action,
    resource: str | Resource,
    error_sig: str | ErrorSignature,
    separator: str = " ",
) -> str:
    """Build a canonical embed string from the three-part taxonomy.

    Args:
        action: One of ``Action`` enum or a free-form uppercase string.
        resource: One of ``Resource`` enum or a free-form uppercase string.
        error_sig: One of ``ErrorSignature`` enum or a free-form uppercase string.
        separator: Token separator between parts. Must match SOP ingest config.

    Returns:
        A deterministic, normalized string ready for ``OllamaClient.embed()``.

    Example::

        s = build_canonical_query(
            action=Action.CHECK,
            resource=Resource.REDIS,
            error_sig=ErrorSignature.OOM_KILLED,
        )
        # → "CHECK REDIS OOM_KILLED"
    """
    q = CanonicalQuery(
        action=str(action.value if isinstance(action, Action) else action).upper().strip(),
        resource=str(resource.value if isinstance(resource, Resource) else resource).upper().strip(),
        error_sig=str(error_sig.value if isinstance(error_sig, ErrorSignature) else error_sig).upper().strip(),
    )
    return q.to_embed_string(separator=separator)


def infer_error_hint_from_promql(promql: str) -> str:
    """Derive error_hint for canonical_query_from_rule_name from the trigger instant query."""
    p = (promql or "").lower()
    if "crashloop" in p or "crash_loop" in p or "crashloopbackoff" in p.replace("_", ""):
        return "crash_loop_backoff"
    if "imagepull" in p or "image_pull" in p or "errimagepull" in p.replace("_", ""):
        return "image_pull"
    if "pending" in p and "kube_pod" in p:
        return "pending"
    if "notready" in p or "not_ready" in p:
        return "not_ready"
    if "oom" in p or "killed" in p:
        return "oom_killed"
    if "throttl" in p:
        return "cpu_throttle"
    return "metric_anomaly"


def canonical_query_from_rule_name(
    rule_name: str,
    target: str = "",
    error_hint: str = "",
    *,
    promql_context: str = "",
) -> str:
    """Best-effort mapping from a Prometheus rule name / alert name to Canonical Query.

    Used by ``evaluate_proactive_triggers`` to map AnomalyEvent → RAG embed.
    Falls back to `DIAGNOSE / CLUSTER / UNKNOWN` if mapping is ambiguous.

    Args:
        rule_name: Prometheus alert/rule name (e.g. ``"KubePodOOMKilled"``).
        target: Optional target label (pod name, node name, service name).
        error_hint: Optional hint from alert annotations for error signature.
    """
    name_lower = (rule_name or "").lower()
    hint_lower = (error_hint or "").lower()
    promql_lower = (promql_context or "").lower()

    # --- Action ---
    if any(k in name_lower for k in ("restart", "rollout", "crash")):
        action = Action.RESTART
    elif any(k in name_lower for k in ("scale", "replicas")):
        action = Action.SCALE
    elif any(k in name_lower for k in ("drain", "evict")):
        action = Action.DRAIN
    elif any(k in name_lower for k in ("rollback",)):
        action = Action.ROLLBACK
    else:
        action = Action.DIAGNOSE

    # --- Resource ---
    tgt_lower = (target or "").lower()
    if "redis" in tgt_lower or "redis" in name_lower:
        resource = Resource.REDIS
    elif "pgvector" in tgt_lower or "pgvector" in name_lower or "ragdb" in tgt_lower:
        resource = Resource.PGVECTOR
    elif "node" in name_lower or "node" in tgt_lower:
        resource = Resource.NODE
    elif "deployment" in name_lower:
        resource = Resource.DEPLOYMENT
    elif "ingress" in name_lower:
        resource = Resource.INGRESS
    elif "pod" in name_lower or "container" in name_lower:
        resource = Resource.POD
    elif "namespace" in name_lower:
        resource = Resource.NAMESPACE
    elif "volume" in name_lower or "pvc" in name_lower or "disk" in name_lower:
        resource = Resource.VOLUME
    elif "service" in name_lower:
        resource = Resource.SERVICE
    else:
        resource = Resource.CLUSTER

    # PrometheusProactiveThreshold + PromQL context → richer taxonomy than CLUSTER/UNKNOWN
    if "proactivethreshold" in name_lower or "prometheusproactive" in name_lower:
        if "kube_pod" in promql_lower or "container" in promql_lower or "pod" in promql_lower:
            resource = Resource.POD
        if "deployment" in promql_lower:
            resource = Resource.DEPLOYMENT
        if "node" in promql_lower and "kube" not in promql_lower:
            resource = Resource.NODE

    # --- Error signature ---
    combined = name_lower + " " + hint_lower + " " + promql_lower
    if "oom" in combined or "killed" in combined:
        error_sig = ErrorSignature.OOM_KILLED
    elif "throttl" in combined or "cpu" in combined:
        error_sig = ErrorSignature.CPU_THROTTLE
    elif "crashloop" in combined or "crash_loop" in combined or "backoff" in combined:
        error_sig = ErrorSignature.CRASH_LOOP
    elif "imagepull" in combined or "image_pull" in combined:
        error_sig = ErrorSignature.IMAGE_PULL
    elif "pending" in combined:
        error_sig = ErrorSignature.PENDING
    elif "notready" in combined or "not_ready" in combined:
        error_sig = ErrorSignature.NOT_READY
    elif "disk" in combined or "full" in combined or "evict" in combined:
        error_sig = ErrorSignature.DISK_FULL
    elif "timeout" in combined or "network" in combined:
        error_sig = ErrorSignature.NETWORK_TIMEOUT
    elif "dns" in combined:
        error_sig = ErrorSignature.DNS_FAIL
    elif "probe" in combined:
        error_sig = ErrorSignature.PROBE_FAIL
    elif "replica" in combined or "mismatch" in combined:
        error_sig = ErrorSignature.REPLICA_MISMATCH
    elif "latency" in combined or "slow" in combined:
        error_sig = ErrorSignature.LATENCY_HIGH
    elif "connection" in combined or "refused" in combined:
        error_sig = ErrorSignature.CONNECTION_REFUSED
    elif "high" in combined or "load" in combined:
        error_sig = ErrorSignature.HIGH_LOAD
    else:
        error_sig = ErrorSignature.UNKNOWN

    return build_canonical_query(action=action, resource=resource, error_sig=error_sig)
