"""TDD for RAG chunk freshness + DEPRECATED_RISK resolution (plan step 4).

Live > RAG > LLM precedence is code-hard: a recalled chunk whose cluster_version
disagrees with the live cluster is labelled DEPRECATED_RISK to force read-only
re-verification before the advisory trusts it.
"""

from __future__ import annotations

from rag.rag_freshness import (
    FRESHNESS_DEPRECATED_RISK,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    assess_recall_freshness,
    stamp_freshness,
)

_NOW = "2026-06-18T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# stamp_freshness — write path                                                #
# --------------------------------------------------------------------------- #


def test_stamp_adds_ingested_at_and_cluster_version_without_mutating_input():
    original = {"text": "Redis OOM runbook", "source": "post-mortem"}
    stamped = stamp_freshness(original, cluster_version="v1.29.4", now_iso=_NOW)

    assert stamped["ingested_at"] == _NOW
    assert stamped["cluster_version"] == "v1.29.4"
    # immutability: original untouched
    assert "ingested_at" not in original
    assert "cluster_version" not in original


def test_stamp_preserves_existing_ingested_at():
    original = {"text": "x", "ingested_at": "2020-01-01T00:00:00+00:00"}
    stamped = stamp_freshness(original, cluster_version="v1.29.4", now_iso=_NOW)
    assert stamped["ingested_at"] == "2020-01-01T00:00:00+00:00"
    assert stamped["cluster_version"] == "v1.29.4"


def test_stamp_no_cluster_version_leaves_field_absent():
    stamped = stamp_freshness({"text": "x"}, cluster_version=None, now_iso=_NOW)
    assert stamped["ingested_at"] == _NOW
    assert "cluster_version" not in stamped


# --------------------------------------------------------------------------- #
# assess_recall_freshness — read path (Live > RAG precedence)                 #
# --------------------------------------------------------------------------- #


def test_version_mismatch_is_deprecated_risk():
    payload = {"cluster_version": "v1.28.0", "ingested_at": _NOW}
    verdict = assess_recall_freshness(
        payload, live_cluster_version="v1.29.4", now_iso=_NOW, max_age_sec=86400
    )
    assert verdict.label == FRESHNESS_DEPRECATED_RISK
    assert verdict.requires_reverify is True
    assert "v1.28.0" in verdict.reason and "v1.29.4" in verdict.reason


def test_version_match_and_recent_is_fresh():
    payload = {"cluster_version": "v1.29.4", "ingested_at": _NOW}
    verdict = assess_recall_freshness(
        payload, live_cluster_version="v1.29.4", now_iso=_NOW, max_age_sec=86400
    )
    assert verdict.label == FRESHNESS_FRESH
    assert verdict.requires_reverify is False


def test_version_match_but_aged_is_stale_soft():
    payload = {
        "cluster_version": "v1.29.4",
        "ingested_at": "2026-06-01T00:00:00+00:00",  # ~17 days before _NOW
    }
    verdict = assess_recall_freshness(
        payload, live_cluster_version="v1.29.4", now_iso=_NOW, max_age_sec=86400
    )
    assert verdict.label == FRESHNESS_STALE
    # stale is a soft warning, not a hard re-verify gate
    assert verdict.requires_reverify is False


def test_missing_metadata_is_unknown_not_hard_block():
    # legacy entries (no freshness stamp) must stay backward-compatible
    payload = {"text": "legacy SOP"}
    verdict = assess_recall_freshness(
        payload, live_cluster_version="v1.29.4", now_iso=_NOW, max_age_sec=86400
    )
    assert verdict.label == FRESHNESS_UNKNOWN
    assert verdict.requires_reverify is False


def test_unknown_live_version_cannot_deprecate():
    # if we don't know the live cluster version, we cannot prove drift
    payload = {"cluster_version": "v1.28.0", "ingested_at": _NOW}
    verdict = assess_recall_freshness(
        payload, live_cluster_version=None, now_iso=_NOW, max_age_sec=86400
    )
    assert verdict.label != FRESHNESS_DEPRECATED_RISK
    assert verdict.requires_reverify is False


def test_malformed_ingested_at_does_not_crash():
    payload = {"cluster_version": "v1.29.4", "ingested_at": "not-a-date"}
    verdict = assess_recall_freshness(
        payload, live_cluster_version="v1.29.4", now_iso=_NOW, max_age_sec=86400
    )
    # cannot compute age → treat version-matched entry as fresh-unknown-age, never crash
    assert verdict.label in (FRESHNESS_FRESH, FRESHNESS_UNKNOWN)
    assert verdict.requires_reverify is False
