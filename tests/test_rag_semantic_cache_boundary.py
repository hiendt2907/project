"""Tests for RAG score_threshold boundary filtering in _docs_to_points."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rag.redis_vector_store import _docs_to_points


def _fake_doc(score_distance: float, doc_id: str = "test:abc123", payload: str = "{}") -> Any:
    """Create a mock Redis search doc with a COSINE distance value."""
    doc = SimpleNamespace()
    doc.__score = str(score_distance)   # Redis returns as string
    doc.id = doc_id
    doc.payload = payload
    return doc


# ---------------------------------------------------------------------------
# Threshold boundary: 0.400
# ---------------------------------------------------------------------------

def test_score_below_threshold_is_excluded():
    """Score 0.394 (< 0.400) must be filtered out."""
    doc = _fake_doc(score_distance=1.0 - 0.394, doc_id="test:low")  # distance=0.606
    pts = _docs_to_points([doc], score_threshold=0.400)
    assert pts == [], f"Expected empty, got {pts}"


def test_score_exactly_at_threshold_is_included():
    """Score == 0.400 must be included (boundary is inclusive)."""
    doc = _fake_doc(score_distance=1.0 - 0.400, doc_id="test:exact")  # distance=0.600
    pts = _docs_to_points([doc], score_threshold=0.400)
    assert len(pts) == 1, f"Expected 1 point, got {pts}"
    assert abs(pts[0].score - 0.400) < 1e-9


def test_score_above_threshold_is_included():
    """Score 0.401 (> 0.400) must be included."""
    doc = _fake_doc(score_distance=1.0 - 0.401, doc_id="test:high")  # distance=0.599
    pts = _docs_to_points([doc], score_threshold=0.400)
    assert len(pts) == 1
    assert pts[0].score > 0.400


def test_no_threshold_returns_all():
    """score_threshold=None must return all docs regardless of score."""
    docs = [
        _fake_doc(0.999, "test:very_low"),
        _fake_doc(0.001, "test:very_high"),
    ]
    pts = _docs_to_points(docs, score_threshold=None)
    assert len(pts) == 2


def test_mixed_docs_only_high_scores_pass():
    """Multiple docs — only those with score >= 0.400 pass."""
    docs = [
        _fake_doc(1.0 - 0.394, "test:1"),  # excluded
        _fake_doc(1.0 - 0.400, "test:2"),  # included
        _fake_doc(1.0 - 0.401, "test:3"),  # included
        _fake_doc(1.0 - 0.950, "test:4"),  # included
    ]
    pts = _docs_to_points(docs, score_threshold=0.400)
    assert len(pts) == 3
    ids = {p.id for p in pts}
    assert "1" in ids or "test:1" not in {p.id for p in pts}
    assert all(p.score >= 0.400 for p in pts)


def test_score_converts_distance_correctly():
    """score = 1.0 - distance; verify the conversion is applied."""
    distance = 0.35
    expected_score = 1.0 - distance
    doc = _fake_doc(distance, "test:conv")
    pts = _docs_to_points([doc], score_threshold=None)
    assert len(pts) == 1
    assert abs(pts[0].score - expected_score) < 1e-9
