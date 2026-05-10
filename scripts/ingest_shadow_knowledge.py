#!/usr/bin/env python3
"""Build markdown knowledge chunks for Shadow OS RAG ingestion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _chunks(text: str, *, size: int = 1400, overlap: int = 200) -> list[str]:
    out: list[str] = []
    s = (text or "").strip()
    if not s:
        return out
    i = 0
    while i < len(s):
        j = min(len(s), i + size)
        out.append(s[i:j])
        if j >= len(s):
            break
        i = max(0, j - overlap)
    return out


def _iter_md_files(root: Path) -> list[Path]:
    return sorted([p for p in root.rglob("*.md") if p.is_file()])


def main() -> None:
    p = argparse.ArgumentParser(description="Export docs/runbooks/project-memory markdown chunks for RAG")
    p.add_argument("--workspace", default=".")
    p.add_argument("--output", default="reports/shadow_rag_chunks.jsonl")
    args = p.parse_args()

    ws = Path(args.workspace).resolve()
    sources = [
        ws / "docs" / "runbooks",
        ws / "docs",
        ws / "docs" / "reports",
    ]
    rows: list[dict[str, object]] = []
    for src in sources:
        if not src.exists():
            continue
        for fp in _iter_md_files(src):
            rel = fp.relative_to(ws).as_posix()
            text = fp.read_text(encoding="utf-8", errors="replace")
            for idx, ck in enumerate(_chunks(text), start=1):
                rows.append(
                    {
                        "id": f"{rel}#chunk-{idx}",
                        "text": ck,
                        "metadata": {
                            "doc_type": "runbook" if "runbooks/" in rel else "doc",
                            "source_path": rel,
                            "scope": "node/workload/app",
                            "env_mode": "shadow_os",
                            "confidence_tier": "verified" if "knownbase" in rel or "project-memory" in rel else "reference",
                        },
                    }
                )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} chunks -> {out}")


if __name__ == "__main__":
    main()
