"""Unit tests for scripts/omni_redis_shadow_jsonl_exporter.py (no live Redis)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "omni_redis_shadow_jsonl_exporter",
    _ROOT / "scripts" / "omni_redis_shadow_jsonl_exporter.py",
)
assert _SPEC and _SPEC.loader
ex = importlib.util.module_from_spec(_SPEC)
sys.modules["omni_redis_shadow_jsonl_exporter"] = ex
_SPEC.loader.exec_module(ex)


def test_load_manifest_merges_last(tmp_path: Path) -> None:
    p = tmp_path / "m.jsonl"
    p.write_text(
        '{"trace_id":"a","review_status":"PENDING"}\n{"trace_id":"a","review_status":"VERIFIED_SUCCESS"}\n',
        encoding="utf-8",
    )
    m = ex._load_manifest(p)
    assert m["a"]["review_status"] == "VERIFIED_SUCCESS"


def test_build_record_merge_manifest() -> None:
    rec = ex.build_record(
        trace_id="t1",
        raw='{"hypotheses":[]}',
        manifest_row={"review_status": "VERIFIED_SUCCESS", "diagnostic_snapshot": {"z": 1}},
        redis_key="omni:selflearn:shadow:t1",
        ttl_sec=3600,
    )
    assert rec["trace_id"] == "t1"
    assert rec["review_status"] == "VERIFIED_SUCCESS"
    assert rec["diagnostic_snapshot"] == {"z": 1}
    assert rec["ttl_remaining_sec"] == 3600


def test_trace_from_key() -> None:
    assert ex._trace_from_key("omni:selflearn:shadow:abc") == "abc"


def test_main_with_fakeredis(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fakeredis = pytest.importorskip("fakeredis")
    fake = fakeredis.FakeRedis(decode_responses=True)
    fake.setex(
        "omni:selflearn:shadow:tid1",
        5000,
        json.dumps({"hypotheses": [{"name": "x"}]}),
    )

    monkeypatch.setattr(ex.redis_lib.Redis, "from_url", lambda url, **kw: fake)

    out = tmp_path / "o.jsonl"
    monkeypatch.setenv("OMNI_REDIS_URL", "redis://unused")
    sys.argv = [
        "omni_redis_shadow_jsonl_exporter.py",
        "--trace-ids",
        "tid1",
        "-o",
        str(out),
    ]
    assert ex.main() == 0
    line = out.read_text(encoding="utf-8").strip().splitlines()[0]
    doc = json.loads(line)
    assert doc["trace_id"] == "tid1"
    assert doc["shadow_artifact"]["hypotheses"][0]["name"] == "x"


def test_require_verified_skips_without_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fakeredis = pytest.importorskip("fakeredis")
    fake = fakeredis.FakeRedis(decode_responses=True)
    fake.setex("omni:selflearn:shadow:tid1", 5000, "{}")

    monkeypatch.setattr(ex.redis_lib.Redis, "from_url", lambda url, **kw: fake)

    out = tmp_path / "o.jsonl"
    sys.argv = [
        "omni_redis_shadow_jsonl_exporter.py",
        "--trace-ids",
        "tid1",
        "--require-verified",
        "-o",
        str(out),
    ]
    assert ex.main() == 0
    assert out.read_text(encoding="utf-8").strip() == ""
