"""llm_context_budget helpers."""

from __future__ import annotations

from types import SimpleNamespace

from workers.llm_context_budget import effective_reply_max_words, truncate_for_llm, truncate_to_words


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


def test_effective_reply_max_words_min_of_concise_and_summary() -> None:
    ws = SimpleNamespace(omni_summary_max_words=100, omni_concise_reply_max_words=30)
    assert effective_reply_max_words(ws) == 30
    ws2 = SimpleNamespace(omni_summary_max_words=20, omni_concise_reply_max_words=30)
    assert effective_reply_max_words(ws2) == 20


def test_truncate_to_words() -> None:
    assert truncate_to_words("a b c d e", 3) == "a b c"
    assert truncate_to_words("short", 30) == "short"
