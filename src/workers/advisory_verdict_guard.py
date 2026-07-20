"""Deterministic verdict consistency guard (SDK/METRIC CONSISTENCY moved out of prompt).

The prompt rule "live probes PASSED + no acute exhaustion → never URGENT/CRITICAL"
sat beyond the production prompt clip, so the model never saw it (trace
gw-prom-84cd18edddb2 emitted a fabricated advisory precisely on healthy
evidence). Prompt rules are advisory; this gate is enforcement: an extreme
verdict requires at least one concrete failure signal in the evidence text.
"""

from __future__ import annotations

import logging
import re

from pkg.reasoning.analyst_advisory_schema import AnalystAdvisory

logger = logging.getLogger(__name__)

_EXTREME_VERDICTS = frozenset({"URGENT", "CRITICAL"})
_SIGMA_ANOMALY_THRESHOLD = 3.0

# Concrete failure vocabulary across all 4 lanes. Word-ish boundaries where the
# token could collide with prose or numbers (HTTP codes inside IPs, etc.).
_FAILURE_MARKER_RES = tuple(
    re.compile(pat, re.I)
    for pat in (
        r"\bfailed\b",
        r"\bfailure\b",
        r"\boomkilled\b",
        r"\bcrashloopbackoff\b",
        r"\bimagepullbackoff\b",
        r"\bnotready\b",
        r"\bevict(?:ed|ion)\b",
        r"\bconnection refused\b",
        r"\btimed?[ -]?out\b",
        r"\bexit code\b",
        r"\bpanic\b",
        r"\btraceback\b",
        r"\bdeadlock\b",
        r"\breplication lag\b",
        r"\bstale file handle\b",
        r"\bunreachable\b",
        r"\bdown\b",
        r"\bbreach(?:ed)?\b",
        r"\bmismatch\b",
        r"\bintegrity\b",
        r"\bchain gap\b",
        r"\bexfil\w*\b",
        r"\bmalware\b",
        r"\bddos\b",
        r"\bransomware\b",
        r"\blateral[_ ]movement\b",
        r"\bsiem_category\s*=",
        r"\battack\b",
        r"\bunauthorized\b",
        r"\bdenied\b",
        r"\banomaly\b",
        r"\bsurge\b",
        r"\berror rate\b",
        r"(?<![\d.])(?:429|500|502|503|504)(?![\d.])",
        r"\b(?:9[1-9]|100)(?:\.\d+)?%",  # partition/resource >=91% full
    )
)
_Z_SCORE_RE = re.compile(r"\bz_(?:cpu|mem|disk)\s*=\s*([+-]?\d+(?:\.\d+)?)")
_GUARD_NOTE = (
    "verdict guard: evidence không có failure signal cụ thể — verdict/forecast bị "
    "hạ về mức thận trọng"
)


def evidence_has_failure_signal(evidence_text: str) -> bool:
    """True when the evidence contains at least one concrete failure indicator."""
    text = evidence_text or ""
    for zm in _Z_SCORE_RE.finditer(text):
        try:
            if abs(float(zm.group(1))) >= _SIGMA_ANOMALY_THRESHOLD:
                return True
        except ValueError:
            continue
    return any(rx.search(text) for rx in _FAILURE_MARKER_RES)


def apply_verdict_consistency_guard(
    advisory: AnalystAdvisory, evidence_text: str
) -> tuple[AnalystAdvisory, bool]:
    """Return (new advisory, fired). Downgrade URGENT/CRITICAL lacking any failure signal."""
    if advisory.verdict not in _EXTREME_VERDICTS:
        return advisory, False
    if evidence_has_failure_signal(evidence_text):
        return advisory, False

    capped = [
        f.model_copy(
            update={
                "severity": "degraded" if f.severity in ("critical", "catastrophic") else f.severity,
                "confidence": "low",
            }
        )
        for f in advisory.forecast.forecasts
    ]
    gated = advisory.model_copy(
        update={
            "verdict": "INVESTIGATE",
            "forecast": advisory.forecast.model_copy(
                update={"forecasts": capped, "note": _GUARD_NOTE}
            ),
        }
    )
    logger.warning(
        "event=advisory_verdict_guard_fired trace=%s original_verdict=%s",
        advisory.trace_id,
        advisory.verdict,
    )
    return gated, True
