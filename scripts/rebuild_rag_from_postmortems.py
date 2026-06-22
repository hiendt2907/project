"""Rebuild RAG SOP entries from real incident post-mortems (plan step 5).

Reads docs/post-mortems/*.md, parses each into an ``omni:rag:sop:{tenant_id}``
entry (alert_context + remediation tool + arg KEYS only — never arg values).
This hash is a data-at-rest ledger (audit/training-data source) — it is not
read by the live recall path (see ``src/workers/archivist.py``).

Low-signal stubs (alert=unknown AND no namespace/workload) are skipped, and the
count of skipped files is logged — no silent truncation. ``omni:rag:sop:{tenant_id}``
is a plain Redis hash lookup so this runs without Ollama embeddings.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/rebuild_rag_from_postmortems.py \
        --dir docs/post-mortems --redis-url redis://localhost:16379/0
    # dry-run (no Redis write):
    ... --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rag.redis_vector_store import DEFAULT_TENANT_ID, validate_tenant_id  # noqa: E402

logger = logging.getLogger("rebuild_rag_from_postmortems")

REDIS_SOP_KEY_FMT = "omni:rag:sop:{tenant_id}"

_FIELD_RE = {
    "alertname": re.compile(r"\*\*Alert:\*\*\s*`([^`]*)`"),
    "namespace": re.compile(r"\*\*Namespace:\*\*\s*`([^`]*)`"),
    "workload": re.compile(r"\*\*Workload:\*\*\s*`([^`]*)`"),
    "tool": re.compile(r"\*\*Remediation tool:\*\*\s*`([^`]*)`"),
    "outcome": re.compile(r"\*\*Outcome:\*\*\s*([A-Z_]+)"),
}
_ARG_KEYS_RE = re.compile(r"\*\*Arg keys used:\*\*\s*(.+)")


def _extract(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    return (m.group(1).strip() if m else "")


def parse_postmortem(text: str, *, slug: str) -> dict[str, Any]:
    """Parse one post-mortem markdown into an omni:rag:sop entry.

    Pure / side-effect free. Only arg KEYS are captured — never values.
    """
    alertname = _extract(_FIELD_RE["alertname"], text)
    namespace = _extract(_FIELD_RE["namespace"], text)
    workload = _extract(_FIELD_RE["workload"], text)
    tool = _extract(_FIELD_RE["tool"], text)
    outcome = _extract(_FIELD_RE["outcome"], text)

    arg_keys: list[str] = []
    am = _ARG_KEYS_RE.search(text)
    if am:
        arg_keys = [k.strip().strip("`") for k in am.group(1).split(",") if k.strip().strip("`")]

    labels: dict[str, str] = {}
    if namespace:
        labels["namespace"] = namespace
    if workload:
        labels["workload"] = workload

    root_cause = (
        f"Prior incident '{slug}': alert={alertname or 'unknown'} resolved via {tool or 'n/a'}"
        f"{f' on workload={workload}' if workload else ''} ({outcome or 'unknown outcome'})."
    )

    return {
        "alert_id": f"pm-{slug}",
        "lane": "SYS_HARD_FAIL",
        "alert_context": {
            "alertname": alertname or "unknown",
            "namespace": namespace,
            "labels": labels,
        },
        "root_cause": root_cause,
        "proposed_remediation": (
            [{"step": f"Apply verified remediation: {tool}", "tool": tool, "arg_keys": arg_keys}]
            if tool
            else []
        ),
        "outcome": outcome or "unknown",
        "source": "post-mortem",
    }


def has_signal(entry: dict[str, Any]) -> bool:
    """True when the entry carries enough signal to be worth recalling.

    A stub with alert=unknown AND no namespace/workload contributes only noise.
    """
    ctx = entry.get("alert_context", {})
    alertname = str(ctx.get("alertname") or "").strip().lower()
    namespace = str(ctx.get("namespace") or "").strip()
    workload = str(ctx.get("labels", {}).get("workload") or "").strip()
    has_named_alert = bool(alertname) and alertname != "unknown"
    return has_named_alert or bool(namespace) or bool(workload)


def build_entries(pm_dir: Path) -> tuple[list[dict[str, Any]], int]:
    """Parse all *.md in *pm_dir*; return (signal_entries, skipped_count)."""
    entries: list[dict[str, Any]] = []
    skipped = 0
    for md in sorted(pm_dir.glob("*.md")):
        slug = md.stem
        try:
            text = md.read_text()
        except Exception as e:
            logger.warning("skip unreadable %s: %s", md.name, e)
            skipped += 1
            continue
        entry = parse_postmortem(text, slug=slug)
        if has_signal(entry):
            entries.append(entry)
        else:
            skipped += 1
    return entries, skipped


async def run(
    pm_dir: Path, redis_url: str, *, dry_run: bool, tenant_id: str = DEFAULT_TENANT_ID
) -> int:
    entries, skipped = build_entries(pm_dir)
    logger.info(
        "parsed post-mortems: signal_entries=%d skipped_low_signal=%d", len(entries), skipped
    )
    if dry_run:
        for e in entries:
            logger.info("would ingest %s alert=%s", e["alert_id"], e["alert_context"]["alertname"])
        return 0

    import redis.asyncio as aioredis

    sop_key = REDIS_SOP_KEY_FMT.format(tenant_id=validate_tenant_id(tenant_id))
    r = aioredis.from_url(redis_url, decode_responses=True)
    try:
        await r.ping()
    except Exception as e:
        logger.error("redis connect failed: %s", e)
        return 1
    pipe = r.pipeline(transaction=False)
    for e in entries:
        pipe.hset(sop_key, e["alert_id"], json.dumps(e, ensure_ascii=False))
    await pipe.execute()
    hlen = await r.hlen(sop_key)
    logger.info("rebuild complete: tenant_id=%s ingested=%d redis_hlen=%d", tenant_id, len(entries), hlen)
    await r.aclose()
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rebuild omni:rag:sop:{tenant_id} from post-mortems")
    p.add_argument("--dir", default="docs/post-mortems")
    p.add_argument("--redis-url", default=os.environ.get("OMNI_REDIS_URL", "redis://localhost:16379/0"))
    p.add_argument("--tenant-id", default=DEFAULT_TENANT_ID, help="Tenant isolation key (default: 'default')")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(None)
    rc = asyncio.run(run(Path(args.dir), args.redis_url, dry_run=args.dry_run, tenant_id=args.tenant_id))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
