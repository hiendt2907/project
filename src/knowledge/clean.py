from __future__ import annotations

import re
from html.parser import HTMLParser


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        t = data.strip()
        if t:
            self._chunks.append(t)

    def get_text(self) -> str:
        return " ".join(self._chunks)


def clean_html(raw: str) -> str:
    """Strip tags and collapse whitespace — never pass raw HTML to embed."""
    stripper = _HTMLStripper()
    stripper.feed(raw)
    stripper.close()
    text = stripper.get_text()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_plain(raw: str) -> str:
    """Normalize whitespace; optional nav line removal for markdown."""
    t = re.sub(r"\s+", " ", raw).strip()
    return t


def assert_no_embed_raw_html(text: str) -> None:
    """Hard guard: refuse strings that still look like HTML documents."""
    s = (text or "").lstrip()
    if s[:512].lower().find("<html") >= 0 or s[:512].lower().find("<!doctype html") >= 0:
        raise ValueError("refuse_to_embed: raw HTML detected after clean — fix pipeline")
