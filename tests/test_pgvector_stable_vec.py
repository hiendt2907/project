"""pgvector_store — deterministic vectors without hitting network."""

from __future__ import annotations

import math

from rag import pgvector_store


def test_stable_vec_normalized() -> None:
    v = pgvector_store._stable_vec_from_text("phase2 error: OOM")  # noqa: SLF001
    assert len(v) == pgvector_store.EMBED_DIM == 768
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-9)


def test_stable_vec_deterministic() -> None:
    a = pgvector_store._stable_vec_from_text("same")  # noqa: SLF001
    b = pgvector_store._stable_vec_from_text("same")
    assert a == b
