"""RAG chunk freshness + DEPRECATED_RISK resolution (SRE-autonomous plan step 4).

Live > RAG > LLM is CODE-HARD here, not a system-prompt suggestion. A recalled
chunk carries the cluster_version + ingested_at it was written under. At recall
time we compare against the *live* cluster:

  - version disagrees with live    → DEPRECATED_RISK (force read-only re-verify)
  - version matches but chunk aged  → STALE (soft warning, advisory still usable)
  - version matches and recent      → FRESH
  - no freshness metadata / unknown → UNKNOWN (backward-compat, never hard-blocks)

The gate is intentionally conservative on the *block* side: we only force
re-verification when we can positively prove drift (both versions known and
different). Missing metadata never escalates, so the ~1000 legacy chunks keep
working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

FRESHNESS_FRESH = "FRESH"
FRESHNESS_STALE = "STALE"
FRESHNESS_DEPRECATED_RISK = "DEPRECATED_RISK"
FRESHNESS_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FreshnessVerdict:
    """Outcome of comparing a recalled chunk to the live cluster."""

    label: str
    requires_reverify: bool
    reason: str


def stamp_freshness(
    payload: dict[str, Any],
    *,
    cluster_version: str | None,
    now_iso: str,
) -> dict[str, Any]:
    """Return a copy of *payload* stamped with freshness metadata.

    Never mutates the input. ``ingested_at`` is preserved if already present so
    re-upserts do not reset the chunk's age. ``cluster_version`` is only written
    when supplied.
    """
    out = dict(payload)
    out.setdefault("ingested_at", now_iso)
    if cluster_version:
        out["cluster_version"] = str(cluster_version)
    return out


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def assess_recall_freshness(
    payload: dict[str, Any],
    *,
    live_cluster_version: str | None,
    now_iso: str,
    max_age_sec: int,
) -> FreshnessVerdict:
    """Classify a recalled chunk's freshness against the live cluster.

    DEPRECATED_RISK is the only label that sets ``requires_reverify`` — it fires
    strictly when both the chunk version and the live version are known and
    disagree, so drift is *proven*, never assumed.
    """
    chunk_version = payload.get("cluster_version")

    # Proven drift: both versions known and different → force re-verify.
    if chunk_version and live_cluster_version and str(chunk_version) != str(live_cluster_version):
        return FreshnessVerdict(
            label=FRESHNESS_DEPRECATED_RISK,
            requires_reverify=True,
            reason=(
                f"RAG chunk cluster_version={chunk_version} disagrees with live "
                f"cluster_version={live_cluster_version}; read-only re-verify before trusting."
            ),
        )

    ingested = _parse_iso(payload.get("ingested_at"))
    now = _parse_iso(now_iso)

    # No usable freshness metadata at all → backward-compatible unknown.
    if chunk_version is None and ingested is None:
        return FreshnessVerdict(
            label=FRESHNESS_UNKNOWN,
            requires_reverify=False,
            reason="no freshness metadata on chunk (legacy entry)",
        )

    # Age check (version matched or live version unknown).
    if ingested is not None and now is not None:
        age = (now - ingested).total_seconds()
        if age > max_age_sec:
            return FreshnessVerdict(
                label=FRESHNESS_STALE,
                requires_reverify=False,
                reason=f"chunk age {int(age)}s exceeds max_age {max_age_sec}s (version matches live)",
            )
        return FreshnessVerdict(
            label=FRESHNESS_FRESH,
            requires_reverify=False,
            reason="version matches live and chunk within freshness window",
        )

    # Version known/matched but age uncomputable → treat as fresh (cannot prove staleness).
    return FreshnessVerdict(
        label=FRESHNESS_FRESH,
        requires_reverify=False,
        reason="cluster_version consistent with live; age unavailable",
    )
