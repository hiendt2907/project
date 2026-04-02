"""Word cap for Ollama tool text (not character cap)."""

from __future__ import annotations

from workers.ollama_prompts_en import OLLAMA_MAX_OUTPUT_WORDS, truncate_plain_text_to_max_words


def test_truncate_preserves_short_text() -> None:
    s = "one two three"
    assert truncate_plain_text_to_max_words(s) == s


def test_truncate_to_25_words() -> None:
    words = [f"w{i}" for i in range(40)]
    long_s = "  " + "  \n ".join(words) + "  "
    out = truncate_plain_text_to_max_words(long_s)
    assert len(out.split()) == OLLAMA_MAX_OUTPUT_WORDS
    assert out.startswith("w0")


def test_empty_returns_empty() -> None:
    assert truncate_plain_text_to_max_words("") == ""
    assert truncate_plain_text_to_max_words("   ") == ""
