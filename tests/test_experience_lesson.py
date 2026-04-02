"""Lesson truncation budget (~200 tokens heuristic)."""

from __future__ import annotations

from execution.experience import truncate_lesson_to_budget


def test_truncate_lesson_budget() -> None:
    long = "x" * 900
    out = truncate_lesson_to_budget(long, max_chars=650)
    assert len(out) <= 650
    assert out.endswith("...")
