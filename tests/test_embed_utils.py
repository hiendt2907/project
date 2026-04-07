"""Embedding input length guard."""

from pkg.rag.embed_utils import truncate_for_embedding


def test_truncate_for_embedding_caps_length() -> None:
    long = "x" * 10000
    out = truncate_for_embedding(long, max_tokens=512)
    assert len(out) < len(long)
    assert "truncated" in out.lower()


def test_truncate_short_unchanged() -> None:
    s = "namespace=ns pod=p alert HighCPU"
    assert truncate_for_embedding(s, max_tokens=512) == s
