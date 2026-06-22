"""Tests for compliance export endpoints (src/gateway/routes/compliance.py)."""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_block(
    seq: int = 1,
    event_type: str = "ADVISORY_DECISION",
    tenant_id: str = "default",
    timestamp_utc: str | None = None,
    prev_hash: str = "0" * 64,
    signature_hex: str | None = None,
) -> dict:
    ts = timestamp_utc or datetime.now(timezone.utc).isoformat()
    block_hash = f"fakehash{seq:04d}"
    return {
        "seq": seq,
        "event_type": event_type,
        "trace_id": f"trace-{seq:04d}",
        "timestamp_utc": ts,
        "payload_hash": "abc123",
        "prev_hash": prev_hash,
        "block_hash": block_hash,
        "signature_hex": signature_hex,
        "public_key_hex": None,
        "pub_key_version": None,
        "payload": {"action": "test"},
        "tenant_id": tenant_id,
    }


def _make_app(blocks_by_key: dict[str, list[dict]]):
    """Create a FastAPI test app with a mocked Redis state."""
    from fastapi import FastAPI, Depends
    from gateway.routes.compliance import router

    app = FastAPI()

    async def _no_auth():
        return None

    app.include_router(router)

    redis_mock = MagicMock()

    async def _lrange(key, start, end):
        rows = blocks_by_key.get(key, [])
        return [json.dumps(b) for b in rows]

    redis_mock.lrange = _lrange
    app.state.redis = redis_mock
    return app


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_csv_export_returns_correct_mime_type():
    blocks = [_make_block(1)]
    app = _make_app({"audit_chain:blocks": blocks})
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/crat/export?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]


def test_csv_export_has_correct_columns():
    blocks = [_make_block(1), _make_block(2)]
    app = _make_app({"audit_chain:blocks": blocks})
    client = TestClient(app)

    resp = client.get("/crat/export?format=csv")
    assert resp.status_code == 200

    reader = csv.DictReader(io.StringIO(resp.text))
    expected_cols = {"seq", "timestamp", "event_type", "trace_id", "tenant_id", "block_hash", "prev_hash", "has_signature"}
    assert expected_cols == set(reader.fieldnames or [])


def test_csv_export_filters_by_days():
    old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    recent_ts = datetime.now(timezone.utc).isoformat()

    blocks = [
        _make_block(1, timestamp_utc=old_ts),
        _make_block(2, timestamp_utc=recent_ts),
    ]
    app = _make_app({"audit_chain:blocks": blocks})
    client = TestClient(app)

    resp = client.get("/crat/export?format=csv&days=30")
    assert resp.status_code == 200

    reader = csv.DictReader(io.StringIO(resp.text))
    rows = list(reader)
    # Old block (60 days ago) should be excluded when days=30
    assert len(rows) == 1
    assert rows[0]["seq"] == "2"


def test_json_export_returns_list():
    blocks = [_make_block(1), _make_block(2)]
    app = _make_app({"audit_chain:blocks": blocks})
    client = TestClient(app)

    resp = client.get("/crat/export?format=json")
    assert resp.status_code == 200

    data = resp.json()
    assert "blocks" in data
    assert isinstance(data["blocks"], list)
    assert data["total"] == 2


def test_stats_endpoint():
    blocks = [
        _make_block(1, event_type="ADVISORY_DECISION", signature_hex="abc"),
        _make_block(2, event_type="HITL_DECISION", signature_hex=None),
    ]
    app = _make_app({"audit_chain:blocks": blocks})
    client = TestClient(app)

    resp = client.get("/crat/stats")
    assert resp.status_code == 200

    data = resp.json()
    assert "total_blocks" in data
    assert "date_range" in data
    assert "event_type_counts" in data
    assert "has_signature" in data
    assert "chain_valid" in data

    assert data["total_blocks"] == 2
    assert data["event_type_counts"]["ADVISORY_DECISION"] == 1
    assert data["event_type_counts"]["HITL_DECISION"] == 1
    assert data["has_signature"] is True


def test_export_tenant_isolation():
    acme_blocks = [_make_block(1, tenant_id="acme")]
    globex_blocks = [_make_block(2, tenant_id="globex")]

    app = _make_app({
        "audit_chain:acme:blocks": acme_blocks,
        "audit_chain:globex:blocks": globex_blocks,
    })
    client = TestClient(app)

    resp_acme = client.get("/crat/export?format=json&tenant_id=acme")
    assert resp_acme.status_code == 200
    acme_data = resp_acme.json()
    assert acme_data["total"] == 1
    assert acme_data["blocks"][0]["tenant_id"] == "acme"

    resp_globex = client.get("/crat/export?format=json&tenant_id=globex")
    assert resp_globex.status_code == 200
    globex_data = resp_globex.json()
    assert globex_data["total"] == 1
    assert globex_data["blocks"][0]["tenant_id"] == "globex"


def test_stats_chain_valid_detects_tampering():
    """chain_valid should be False when prev_hash linkage breaks."""
    b1 = _make_block(1, prev_hash="0" * 64)
    b1["block_hash"] = "hash001"
    b2 = _make_block(2, prev_hash="WRONG_HASH")  # intentionally wrong
    b2["block_hash"] = "hash002"

    app = _make_app({"audit_chain:blocks": [b1, b2]})
    client = TestClient(app)

    resp = client.get("/crat/stats")
    assert resp.status_code == 200
    assert resp.json()["chain_valid"] is False
