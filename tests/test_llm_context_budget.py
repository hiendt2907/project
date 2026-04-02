"""llm_context_budget helpers."""

from __future__ import annotations

from workers.llm_context_budget import truncate_for_llm


def test_truncate_for_llm_noop_when_short() -> None:
    assert truncate_for_llm("hello", 100, tail=True) == "hello"


def test_truncate_for_llm_tail() -> None:
    s = "a" * 100
    out = truncate_for_llm(s, 20, tail=True)
    assert len(out) == 20
    assert out == s[-20:]


def test_truncate_for_llm_head() -> None:
    s = "b" * 100
    out = truncate_for_llm(s, 15, tail=False)
    assert len(out) == 15
    assert out == s[:15]


def test_truncate_for_llm_zero_max() -> None:
    assert truncate_for_llm("x", 0, tail=True) == ""
