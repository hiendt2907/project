"""Principle-based SIEM reasoning engine.

Replaces the old per-category lookup tables for WHY / VERIFY with a small set of
*reasoning principles* applied to the incident *evidence*. The goal: an incident
whose category or source IP has never been seen before is still reasoned about
correctly — there is no "not in the table -> empty default" dead-end.

The load-bearing scope claim (single vs distributed, internal vs external,
cluster-in-scope vs edge) is derived from observable facts:
  - origin classification of each source address (generalises over any IP),
  - source cardinality (how many distinct origins the evidence actually shows),
  - the confirm-ingress-before-block principle,
  - blast-radius described from observed magnitude (pps / source count), not a
    hardcoded catastrophe.

The threat category, when known, only adds descriptive flavour and decides which
investigation *layer* to point at — it never overrides what the evidence shows.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Origin classification — generalises over ANY address, not a lookup table
# ---------------------------------------------------------------------------

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}\b")


@dataclass(frozen=True)
class OriginClass:
    """How a single source address should be treated by an operator."""

    raw: str
    kind: str  # external_routable | internal_rfc1918 | loopback | link_local | unknown
    is_internal: bool
    is_routable_external: bool

    @property
    def is_known(self) -> bool:
        return self.kind != "unknown"


def classify_origin(ip: str) -> OriginClass:
    """Classify an address by what it implies for response — by *principle*, so a
    never-before-seen IP is still placed correctly."""
    raw = str(ip or "").strip()
    if not raw or raw in ("?", "n/a", "none", "null"):
        return OriginClass(raw, "unknown", is_internal=False, is_routable_external=False)
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return OriginClass(raw, "unknown", is_internal=False, is_routable_external=False)
    if addr.is_loopback:
        return OriginClass(raw, "loopback", is_internal=True, is_routable_external=False)
    if addr.is_link_local:
        return OriginClass(raw, "link_local", is_internal=True, is_routable_external=False)
    if addr.is_private:
        return OriginClass(raw, "internal_rfc1918", is_internal=True, is_routable_external=False)
    # Globally routable (public) — reserved/doc ranges already covered by is_private.
    return OriginClass(raw, "external_routable", is_internal=False, is_routable_external=True)


def is_internal_ip(ip: str) -> bool:
    """Back-compat helper: RFC1918 / loopback / link-local."""
    return classify_origin(ip).is_internal


# ---------------------------------------------------------------------------
# Evidence extraction — pull observable facts out of the incident batch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiemEvidence:
    category: str
    severity: str
    namespace: str
    tenant: str
    incident_id: str
    description: str
    suggested_action: str
    source_ips: tuple[str, ...]   # distinct, in first-seen order
    pps: int | None               # peak packets/requests-per-second if observed


def _coerce_fact(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            import json

            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _distinct(seq: list[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for s in seq:
        s = str(s or "").strip()
        if s and s not in seen and s not in ("?", "n/a", "none", "null"):
            seen.append(s)
    return tuple(seen)


def _parse_pps(text: str) -> int | None:
    """Extract a peak rate like '80k pps' / 'pps 80000' / '80000 req/s' from text."""
    if not text:
        return None
    # `<num>[k|m] pps|req/s|rps` — collect ALL rates, return the peak
    values: list[int] = []
    for pattern in (
        r"(\d+(?:\.\d+)?)\s*([kKmM]?)\s*(?:pps|req/?s|rps|packets?/s)",
        r"(?:pps|rate|req/?s|rps)\D{0,4}(\d+(?:\.\d+)?)\s*([kKmM]?)",
    ):
        for m in re.finditer(pattern, text):
            val = float(m.group(1))
            mult = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
            values.append(int(val * mult))
    return max(values) if values else None


def extract_siem_evidence(
    batch: list[dict[str, Any]], siem_labels: dict[str, str]
) -> SiemEvidence:
    """Collect observable facts from the incident batch. Source IPs are gathered
    from every evidence item (explicit fields + free-text), so cardinality is
    measured from what the evidence shows, not assumed."""
    ctx_fact: dict[str, Any] = {}
    ips: list[str] = []
    text_blob: list[str] = []

    for b in batch:
        fact = _coerce_fact(b.get("extracted_fact"))
        if b.get("probe") == "siem_incident_context" and not ctx_fact:
            ctx_fact = fact
        # explicit IP fields (single or list)
        for key in ("affected_ip", "source_ip", "src_ip", "origin_ip"):
            v = fact.get(key)
            if isinstance(v, str):
                ips.append(v)
            elif isinstance(v, (list, tuple)):
                ips.extend(str(x) for x in v)
        for key in ("source_ips", "src_ips", "affected_ips"):
            v = fact.get(key)
            if isinstance(v, (list, tuple)):
                ips.extend(str(x) for x in v)
        # free text for IP scraping + pps
        for piece in (b.get("raw"), b.get("alert_hint"), fact.get("description")):
            if isinstance(piece, str) and piece:
                text_blob.append(piece)
                ips.extend(_IPV4_RE.findall(piece))
                ips.extend(_IPV6_RE.findall(piece))

    blob = "  ".join(text_blob)
    # keep explicit field IPs first; only count addresses that classify as real IPs
    valid_ips = [ip for ip in ips if classify_origin(ip).is_known]

    return SiemEvidence(
        category=str(ctx_fact.get("category") or siem_labels.get("siem_category", "unknown")),
        severity=str(ctx_fact.get("severity") or siem_labels.get("severity", "")),
        namespace=str(ctx_fact.get("namespace") or siem_labels.get("namespace", "multi-agent")),
        tenant=str(ctx_fact.get("tenant") or siem_labels.get("siem_tenant", "")),
        incident_id=str(ctx_fact.get("incident_id") or siem_labels.get("siem_incident_id", "n/a")),
        description=str(ctx_fact.get("description", "")),
        suggested_action=str(ctx_fact.get("suggested_action", "")),
        source_ips=_distinct(valid_ips),
        pps=_parse_pps(blob),
    )


# ---------------------------------------------------------------------------
# Reasoning principles
# ---------------------------------------------------------------------------


def assess_cardinality(ev: SiemEvidence) -> str:
    """single | multiple | unknown — measured from distinct observed origins."""
    n = len(ev.source_ips)
    if n == 0:
        return "unknown"
    return "single" if n == 1 else "multiple"


def _origin_summary(ev: SiemEvidence) -> str:
    if not ev.source_ips:
        return "no source address captured in evidence"
    kinds = {classify_origin(ip).kind for ip in ev.source_ips}
    n = len(ev.source_ips)
    shown = ", ".join(ev.source_ips[:4]) + (" …" if n > 4 else "")
    if kinds == {"external_routable"}:
        scope = "all external/public"
    elif kinds <= {"internal_rfc1918", "loopback", "link_local"}:
        scope = "all internal (RFC1918/loopback)"
    else:
        scope = "mixed internal+external"
    return f"{n} distinct source(s) [{shown}] — {scope}"


def _magnitude_clause(ev: SiemEvidence) -> str:
    if ev.pps is None:
        return ""
    return f" Observed peak ~{ev.pps:,} pps."


# threat category -> which OS/cluster *layer* the signal lives at (principle, not
# a canned answer). Unknown category -> generic but still non-empty guidance.
_CATEGORY_LAYER: dict[str, str] = {
    "ddos": "node/netfilter conntrack + edge/LB before any K8s object",
    "network_anomaly": "node interface / routing before service-mesh",
    "lateral_movement": "pod-to-pod east-west (NetworkPolicy / pod IPs)",
    "data_exfil": "egress + RBAC of the originating workload",
    "malware": "the suspect pod's process/network behaviour",
    "k8s_threat": "RBAC / pod securityContext / control-plane audit",
    "auth_failure": "auth service logs + service-account tokens",
}
_DEFAULT_LAYER = "the workload/node the evidence points at"


def reason_why(ev: SiemEvidence) -> str:
    """Principle-grounded root-cause statement. The scope claim comes from
    cardinality + origin class, NOT from the category."""
    card = assess_cardinality(ev)
    origin = _origin_summary(ev)
    mag = _magnitude_clause(ev)
    cat = ev.category if ev.category and ev.category != "unknown" else "security incident"

    if card == "single":
        oc = classify_origin(ev.source_ips[0])
        if oc.is_internal:
            return (
                f"{cat}: traffic/activity from a SINGLE INTERNAL source ({oc.raw}, {oc.kind}). "
                "This is NOT a distributed external attack — treat as a compromised or "
                "misbehaving internal host/pod until proven otherwise; do NOT edge-block "
                f"before confirming origin.{mag} Source profile: {origin}."
            )
        return (
            f"{cat}: high-rate traffic from a SINGLE external source ({oc.raw}). "
            "One origin — rate-limiting/blocking that specific source at the edge is "
            "plausible, but first confirm it actually reaches the cluster (it may be "
            f"dropped upstream).{mag} Source profile: {origin}."
        )
    if card == "multiple":
        kinds = {classify_origin(ip).kind for ip in ev.source_ips}
        if kinds <= {"internal_rfc1918", "loopback", "link_local"}:
            return (
                f"{cat}: multiple INTERNAL sources — consistent with lateral movement or a "
                "compromised internal segment, not an external flood. Investigate east-west "
                f"traffic and the affected workloads.{mag} Source profile: {origin}."
            )
        return (
            f"{cat}: multiple distinct external sources — pattern consistent with a "
            "DISTRIBUTED attack; edge/WAF mitigation is appropriate once you confirm the "
            f"traffic terminates inside the cluster.{mag} Source profile: {origin}."
        )
    # unknown origin — never a dead-end: state the gap as the next action
    return (
        f"{cat}: origin not captured in the evidence, so distribution and scope are "
        "UNCONFIRMED. Do NOT assume a distributed external attack or a cluster cause — "
        f"the first job is to identify the source(s).{mag}"
    )


def reason_verify(ev: SiemEvidence) -> list[str]:
    """Scope-confirmation steps derived from principles. Always non-empty and
    generalises to any category/IP."""
    steps: list[str] = []
    ns = ev.namespace or "multi-agent"
    layer = _CATEGORY_LAYER.get(ev.category, _DEFAULT_LAYER)

    # 1. Always: classify the origin(s) — the single decision that flips response.
    if ev.source_ips:
        ip_list = " ".join(ev.source_ips[:4])
        steps.append(
            f"Classify source(s) {ip_list}: external/public vs RFC1918/internal. A single "
            "internal source means investigate that host/pod — it is NOT a distributed "
            "external attack and must not be edge-blocked."
        )
    else:
        steps.append(
            "Identify the source address(es) first — the incident carries no origin, so "
            "neither distribution nor cluster-scope can be assumed yet."
        )

    # 2. Always: confirm-ingress-before-block — does the activity reach the cluster?
    steps.append(
        f"Confirm the activity actually terminates inside the cluster (a pod/service in "
        f"namespace {ns}), not at the node/edge. Inspect {layer}. If it is dropped or "
        "handled upstream, the cluster-scoped HOW-TO below does not apply."
    )

    # 3. Cardinality-specific check — verify the claim the evidence implies.
    card = assess_cardinality(ev)
    if card == "single":
        steps.append(
            "Verify this really is one origin (de-dup NAT/proxy) before describing it as "
            "single-source."
        )
    elif card == "multiple":
        steps.append(
            f"Verify the distinct-source count ({len(ev.source_ips)} seen) to justify a "
            "'distributed' classification before engaging WAF/edge mitigation."
        )
    return steps


def reason_blast_radius(ev: SiemEvidence) -> str:
    """One-line blast-radius described from observed magnitude, not hardcoded."""
    card = assess_cardinality(ev)
    bits: list[str] = []
    if ev.pps is not None:
        bits.append(f"~{ev.pps:,} pps observed")
    bits.append(f"{len(ev.source_ips) or 'unknown'} source(s)")
    bits.append(f"scope claim: {card}")
    return "Blast-radius (from evidence): " + ", ".join(bits) + f"; namespace {ev.namespace}."
