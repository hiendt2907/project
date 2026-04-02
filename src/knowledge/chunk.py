from __future__ import annotations

import re

from knowledge.models import TextChunk


def chunk_by_markdown_headings(text: str, *, max_chars: int = 1200, overlap: int = 80) -> list[TextChunk]:
    """Split on # / ## lines; sub-split long sections by character window."""
    lines = (text or "").splitlines()
    sections: list[tuple[str, list[str]]] = []
    cur_heading = "root"
    buf: list[str] = []
    for ln in lines:
        m = re.match(r"^(#{1,3})\s+(.+)$", ln.strip())
        if m:
            if buf:
                sections.append((cur_heading, buf))
                buf = []
            cur_heading = m.group(2).strip()
        else:
            buf.append(ln)
    if buf:
        sections.append((cur_heading, buf))

    out: list[TextChunk] = []
    idx = 0
    for heading, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        if len(body) <= max_chars:
            out.append(TextChunk(heading_path=heading, body=body, chunk_index=idx))
            idx += 1
            continue
        start = 0
        while start < len(body):
            piece = body[start : start + max_chars]
            out.append(TextChunk(heading_path=heading, body=piece.strip(), chunk_index=idx))
            idx += 1
            start += max_chars - overlap
    return out
