#!/usr/bin/env python3
"""Export omni:selflearn:shadow:{trace_id} from Redis to JSONL, optional manifest merge.

Does not connect to PGVector. Use after lab runs for review / gold dataset workflows.

Env:
  OMNI_REDIS_URL (default redis://localhost:16379/0)

Examples:
  .venv/bin/python scripts/omni_redis_shadow_jsonl_exporter.py \\
    --trace-ids-file traces.txt --out shadow_export.jsonl

  .venv/bin/python scripts/omni_redis_shadow_jsonl_exporter.py \\
    --scan-prefix --require-verified --manifest manifest.jsonl --out gold.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import redis as redis_lib

SHADOW_PREFIX = "omni:selflearn:shadow:"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    """trace_id -> merged row from JSONL (last line wins)."""
    out: dict[str, dict[str, Any]] = {}
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        row = json.loads(line)
        tid = str(row.get("trace_id") or "").strip()
        if tid:
            out[tid] = row
    return out


def _parse_trace_ids(args: argparse.Namespace) -> list[str]:
    ids: list[str] = []
    if args.trace_ids:
        ids.extend([x.strip() for x in args.trace_ids.split(",") if x.strip()])
    if args.trace_ids_file:
        p = Path(args.trace_ids_file)
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ids.append(line)
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in ids:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _scan_keys(r: Any, prefix: str) -> list[str]:
    """Return full Redis keys matching prefix."""
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = r.scan(cursor=cursor, match=f"{prefix}*", count=500)
        keys.extend([k.decode() if isinstance(k, bytes) else k for k in batch])
        if cursor == 0:
            break
    return sorted(set(keys))


def _trace_from_key(key: str) -> str:
    if not key.startswith(SHADOW_PREFIX):
        return ""
    return key[len(SHADOW_PREFIX) :]


def build_record(
    *,
    trace_id: str,
    raw: str | None,
    manifest_row: dict[str, Any] | None,
    redis_key: str,
    ttl_sec: int | None,
) -> dict[str, Any]:
    shadow: Any = None
    if raw:
        try:
            shadow = json.loads(raw)
        except json.JSONDecodeError:
            shadow = {"_parse_error": "invalid_json", "_raw": raw[:2000]}
    rec: dict[str, Any] = {
        "trace_id": trace_id,
        "redis_key": redis_key,
        "exported_at": _utc_now_iso(),
        "shadow_artifact": shadow,
        "ttl_remaining_sec": ttl_sec,
    }
    if manifest_row:
        for k in (
            "review_status",
            "diagnostic_snapshot",
            "scenario_id",
            "chaos_run_id",
            "learning_round",
            "rag_signal",
            "ground_truth",
            "reviewer",
            "reviewed_at",
        ):
            if k in manifest_row and manifest_row[k] is not None:
                rec[k] = manifest_row[k]
    return rec


def main() -> int:
    p = argparse.ArgumentParser(description="Redis shadow self-learn → JSONL exporter.")
    p.add_argument("--redis-url", default=os.environ.get("OMNI_REDIS_URL", "redis://localhost:16379/0"))
    p.add_argument("--trace-ids", default="", help="Comma-separated trace ids")
    p.add_argument("--trace-ids-file", default="", help="File with one trace_id per line")
    p.add_argument(
        "--scan-prefix",
        action="store_true",
        help=f"Export all keys matching {SHADOW_PREFIX}* (ignore trace list if empty)",
    )
    p.add_argument("--manifest", default="", help="JSONL manifest to merge by trace_id")
    p.add_argument(
        "--require-verified",
        action="store_true",
        help="Only emit rows with review_status=VERIFIED_SUCCESS (needs manifest)",
    )
    p.add_argument("-o", "--out", default="-", help="Output file (default stdout)")
    args = p.parse_args()

    r = redis_lib.Redis.from_url(args.redis_url, decode_responses=True)

    manifest: dict[str, dict[str, Any]] = {}
    if args.manifest:
        manifest = _load_manifest(Path(args.manifest))

    traces = _parse_trace_ids(args)
    keys_to_fetch: list[tuple[str, str]] = []

    if args.scan_prefix:
        for key in _scan_keys(r, SHADOW_PREFIX):
            tid = _trace_from_key(key)
            if tid:
                keys_to_fetch.append((tid, key))
    else:
        if not traces:
            print("Provide --trace-ids, --trace-ids-file, or --scan-prefix", file=sys.stderr)
            return 2
        for tid in traces:
            keys_to_fetch.append((tid, f"{SHADOW_PREFIX}{tid}"))

    out_lines: list[str] = []
    for trace_id, redis_key in keys_to_fetch:
        mrow = manifest.get(trace_id)
        if args.require_verified:
            if not mrow or str(mrow.get("review_status") or "").strip() != "VERIFIED_SUCCESS":
                continue
        raw = r.get(redis_key)
        ttl_sec = r.ttl(redis_key)
        if ttl_sec is not None and ttl_sec < 0:
            ttl_sec = None
        rec = build_record(
            trace_id=trace_id,
            raw=raw,
            manifest_row=mrow,
            redis_key=redis_key,
            ttl_sec=ttl_sec,
        )
        out_lines.append(json.dumps(rec, ensure_ascii=False))

    text = "\n".join(out_lines) + ("\n" if out_lines else "")
    if args.out == "-":
        sys.stdout.write(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
