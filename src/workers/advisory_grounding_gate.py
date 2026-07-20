"""Post-hoc grounding gate for the Advisory lane (INV_DIAG_GROUNDED applied to advisories).

Prompt rules alone do not hold for the 7B model: trace gw-prom-84cd18edddb2
(2026-07-15) showed qwen2.5-coder:7b parroting the system prompt's own example
values into a real advisory — root_cause "Pod nginx-test bị OOMKilled do vượt
giới hạn bộ nhớ cgroup" copied verbatim, trace_id left as the literal
placeholder "<copy from input>" — while the evidence batch named no workload at
all. The fabricated advisory was dispatched to Telegram and the CRAT chain.

The LLM only sees the system prompt + evidence_text, so any concrete claim in
its output that does not appear in evidence_text necessarily came from the
prompt template or model memory — fabrication by construction. This gate
deterministically neutralizes such advisories the same way
services/analyst/diagnosis_loop.py does for the remote-agent lane.
"""

from __future__ import annotations

import logging
import re

from pkg.reasoning.analyst_advisory_schema import AnalystAdvisory

logger = logging.getLogger(__name__)

# Unfilled template placeholders (e.g. "<copy from input>", "<ns>/<dep>") are
# fabrication markers regardless of the evidence content.
_PLACEHOLDER_RE = re.compile(r"<[^<>\n]{1,60}>")
_PATH_RE = re.compile(r"(?:/[\w.@+-]+){2,}")
_PCT_RE = re.compile(r"\b\d{1,3}(?:\.\d+)?%")
# Dash-names alone are ambiguous (English prose is full of "out-of-memory",
# "self-resolved", ...). A dash-name is only a groundable OBJECT claim when the
# text asserts it as one: preceded by a K8s kind keyword ("Pod nginx-test") or
# written as a namespace/name pair ("default/nginx-test").
_KIND_NAME_RE = re.compile(
    r"\b(?:pod|deployment|statefulset|daemonset|replicaset|container|namespace"
    r"|node|host|service|svc|workload|cronjob|job)\b[\s:\"'=]*"
    r"([a-z0-9]+(?:-[a-z0-9]+)+)",
    re.I,
)
_NS_SLASH_NAME_RE = re.compile(r"\b([a-z0-9][a-z0-9-]{2,})/([a-z0-9][a-z0-9-]{2,})\b")

_WORKLOAD_UNKNOWN = frozenset({"", "unknown", "n/a", "none"})
_GATE_NOTE = (
    "grounding gate: kết luận gốc chứa claim không có trong evidence — forecast bị "
    "hạ về mức thận trọng"
)


def collect_ungrounded_claims(text: str, evidence_corpus: str) -> list[str]:
    """Return concrete claims in ``text`` that cannot be traced to ``evidence_corpus``.

    Claim classes: unfilled ``<placeholder>`` tokens (always ungrounded), absolute
    paths, percentages, kind-asserted object names ("Pod nginx-test"), and
    namespace/name pairs containing a dash.
    """
    placeholders = set(_PLACEHOLDER_RE.findall(text))
    anchored = (
        set(_PATH_RE.findall(text))
        | set(_PCT_RE.findall(text))
        | {name.lower() for name in _KIND_NAME_RE.findall(text)}
    )
    for m in _NS_SLASH_NAME_RE.finditer(text):
        if "-" in m.group(0):  # plain word/word (e.g. inside a path) is not an object claim
            anchored.update(p for p in m.groups() if len(p) >= 3)
    # Case-insensitive: evidence viết "Ollama"/"Omni-Fullstack" nhưng model diễn đạt
    # thường — casing không phải bằng chứng bịa (regression benchmark case_009).
    corpus = evidence_corpus.lower()
    ungrounded = placeholders | {c for c in anchored if c.lower() not in corpus}
    return sorted(ungrounded)


def _workload_claims(affected_workload: str, evidence_corpus: str) -> list[str]:
    """Ground-check each path segment of ``namespace/workload`` independently."""
    wl = affected_workload.strip()
    if wl.lower() in _WORKLOAD_UNKNOWN:
        return []
    out: set[str] = set(_PLACEHOLDER_RE.findall(wl))
    corpus = evidence_corpus.lower()
    for part in wl.split("/"):
        part = part.strip()
        if len(part) >= 3 and not _PLACEHOLDER_RE.fullmatch(part) and part.lower() not in corpus:
            out.add(part)
    return sorted(out)


def _step_is_contaminated(step: object, ungrounded: list[str]) -> bool:
    """A verification step citing any ungrounded claim is a template echo — drop it."""
    blob = " ".join(
        str(getattr(step, field, "") or "")
        for field in ("command", "rationale", "expected_output")
    ).lower()
    return any(claim.lower() in blob for claim in ungrounded)


def apply_advisory_grounding_gate(
    advisory: AnalystAdvisory, evidence_text: str
) -> tuple[AnalystAdvisory, list[str]]:
    """Return a NEW advisory with fabricated claims neutralized, plus the claim list.

    When any claim in root_cause/affected_workload is not traceable to
    ``evidence_text``: verdict → INVESTIGATE, confidence → low, remediation
    emptied, contaminated verification steps dropped, forecast severities capped
    to degraded. A grounded advisory passes through unchanged.
    """
    ungrounded = sorted(
        set(collect_ungrounded_claims(advisory.root_cause, evidence_text))
        | set(_workload_claims(advisory.affected_workload, evidence_text))
    )
    if not ungrounded:
        return advisory, []

    kept_steps = [
        s for s in advisory.verification_steps if not _step_is_contaminated(s, ungrounded)
    ]
    capped_forecasts = [
        f.model_copy(
            update={
                "severity": "degraded" if f.severity in ("critical", "catastrophic") else f.severity,
                "confidence": "low",
            }
        )
        for f in advisory.forecast.forecasts
    ]
    workload_bad = bool(_workload_claims(advisory.affected_workload, evidence_text))
    gated = advisory.model_copy(
        update={
            "verdict": "INVESTIGATE",
            "confidence": "low",
            "root_cause": f"[UNGROUNDED: {', '.join(ungrounded)}] {advisory.root_cause}",
            "affected_workload": "unknown" if workload_bad else advisory.affected_workload,
            "verification_steps": kept_steps,
            "proposed_remediation": [],
            "forecast": advisory.forecast.model_copy(
                update={"forecasts": capped_forecasts, "note": _GATE_NOTE}
            ),
        }
    )
    logger.warning(
        "event=advisory_grounding_gate_fired trace=%s ungrounded=%s",
        advisory.trace_id,
        ungrounded,
    )
    return gated, ungrounded
