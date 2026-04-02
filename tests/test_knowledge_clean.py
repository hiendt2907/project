"""Knowledge pipeline: clean + chunk guards (no raw HTML to embed)."""

from __future__ import annotations

import pytest

from knowledge.chunk import chunk_by_markdown_headings
from knowledge.clean import assert_no_embed_raw_html, clean_html


def test_clean_html_strips_tags() -> None:
    html = "<html><body><nav>skip</nav><p>Hello  world</p></body></html>"
    t = clean_html(html)
    assert "Hello" in t
    assert "<p>" not in t


def test_assert_no_embed_raw_html_rejects_document() -> None:
    with pytest.raises(ValueError, match="refuse_to_embed"):
        assert_no_embed_raw_html("<!doctype html><html><body>x</body></html>")


def test_chunk_headings() -> None:
    md = "# A\n\ntext a\n\n## B\n\nmore\n"
    chunks = chunk_by_markdown_headings(md, max_chars=500)
    assert len(chunks) >= 1
    assert any("text" in c.body.lower() or "more" in c.body.lower() for c in chunks)
