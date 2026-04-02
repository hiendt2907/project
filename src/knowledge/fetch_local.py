from __future__ import annotations

from pathlib import Path

from knowledge.models import RawDocument


async def fetch_local_markdown(source_id: str, directory: str) -> list[RawDocument]:
    root = Path(directory)
    out: list[RawDocument] = []
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*.md")):
        if path.name.startswith("."):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        key = str(path.relative_to(root))
        out.append(
            RawDocument(
                source_id=source_id,
                source_key=key,
                raw_text=text,
                content_type="markdown",
            )
        )
    return out
