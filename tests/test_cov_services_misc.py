"""Coverage tests for the leftover services modules (no unittest.mock):

* services.evidence_adapter.siem_adapter — secondary network envelope branch
* services.evidence_adapter.siem_crat_bridge — happy path with fakeredis + FakeKafka
* services.playbook.matcher — the ``labels is not a dict`` continue branch
* services.analyst.__main__ — invoke main() boundary check
"""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest

# worker.py guards on env at import time — set before importing the evidence_adapter
# package which transitively pulls worker.py through __init__.py.
os.environ.setdefault("ADAPTER_REDIS_URL", "redis://localhost:6379")

from services.evidence_adapter.siem_adapter import SIEMEvidenceAdapter
from services.evidence_adapter.siem_crat_bridge import write_siem_remediation_to_crat
from services.playbook.matcher import PlaybookMatcher
from services.playbook.models import Playbook, PlaybookStep


# ── SIEM adapter: secondary network envelope ─────────────────────────────────

def test_siem_adapter_produces_network_envelope_when_affected_ip_present():
    adapter = SIEMEvidenceAdapter()
    event = {
        "id": "inc-network-1",
        "category": "network_anomaly",
        "severity": "critical",
        "tenant_id": "tenant-net",
        "description": "Unusual east-west traffic",
        "affected_ip": "10.0.0.5",
        "raw_log": "src=10.0.0.5 dst=10.0.0.99 bytes=99999",
    }
    envelopes = adapter.to_evidence(event)
    assert len(envelopes) == 2
    primary, network = envelopes
    assert primary["probe"] == "siem_incident"
    assert network["probe"] == "siem_network_event"
    assert "10.0.0.5" in network["alert_hint"]
    assert network["extracted_fact"]["affected_ip"] == "10.0.0.5"
    assert "src=10.0.0.5" in network["raw"]
    # Network envelope shares trace_id with the primary one.
    assert network["trace_id"] == primary["trace_id"]


def test_siem_adapter_no_network_envelope_without_affected_ip():
    adapter = SIEMEvidenceAdapter()
    event = {
        "id": "inc-no-ip",
        "category": "k8s_threat",
        "severity": "high",
        "tenant_id": "tenant-x",
        "description": "Privileged container created",
    }
    envelopes = adapter.to_evidence(event)
    assert len(envelopes) == 1


def test_siem_adapter_severity_map_falls_back_to_warning():
    adapter = SIEMEvidenceAdapter()
    env = adapter.to_evidence({
        "id": "inc-unknown-sev",
        "category": "ddos",
        "severity": "totally-unknown",
        "tenant_id": "t",
    })[0]
    assert env["extracted_fact"]["severity"] == "warning"


def test_siem_adapter_category_alertname_fallback():
    """Unknown categories must map through the title()-fallback path."""
    adapter = SIEMEvidenceAdapter()
    env = adapter.to_evidence({
        "id": "inc-novel",
        "category": "novel_attack",
        "severity": "low",
        "tenant_id": "t",
    })[0]
    assert env["alert_rule"] == "SIEMNovelattack"


# ── siem_crat_bridge ──────────────────────────────────────────────────────────

class _FakeKafka:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], bytes | None]] = []

    async def send_dict(self, topic: str, message: dict[str, Any], key: bytes | None = None) -> None:
        self.sent.append((topic, message, key))


@pytest.fixture(autouse=True)
def _disable_audit_signing(monkeypatch):
    monkeypatch.delenv("OMNI_AUDIT_PRIVATE_KEY_PATH", raising=False)
    from services.audit_ledger import signer as _s
    _s._load_private_key.cache_clear()
    yield
    _s._load_private_key.cache_clear()


@pytest.mark.asyncio
async def test_write_siem_remediation_to_crat_persists_block():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    kafka = _FakeKafka()
    ctx = SimpleNamespace(redis=redis, kafka=kafka)

    block = await write_siem_remediation_to_crat(
        incident_id="incident-42",
        category="ddos",
        action_taken="kubectl scale --replicas=0 deploy/evil",
        outcome="success",
        ctx=ctx,
    )

    assert block["event_type"] == "SIEM_REMEDIATION"
    assert block["trace_id"] == "incident-42"
    assert block["payload"] == {
        "category": "ddos",
        "action_taken": "kubectl scale --replicas=0 deploy/evil",
        "outcome": "success",
    }
    # Chain head + Kafka publish both occurred.
    assert await redis.get("audit_chain:head_hash") == block["block_hash"]
    assert len(kafka.sent) == 1
    topic, msg, key = kafka.sent[0]
    assert topic == "omni-audit-chain"
    assert msg["event_type"] == "SIEM_REMEDIATION"
    assert key == str(block["seq"]).encode()


@pytest.mark.asyncio
async def test_write_siem_remediation_to_crat_chain_continues():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    kafka = _FakeKafka()
    ctx = SimpleNamespace(redis=redis, kafka=kafka)
    b1 = await write_siem_remediation_to_crat(
        incident_id="i-1", category="malware", action_taken="quarantine",
        outcome="success", ctx=ctx,
    )
    b2 = await write_siem_remediation_to_crat(
        incident_id="i-2", category="auth_failure", action_taken="block_ip",
        outcome="rejected", ctx=ctx,
    )
    assert b2["seq"] == b1["seq"] + 1
    assert b2["prev_hash"] == b1["block_hash"]


# ── PlaybookMatcher: labels-not-dict branch (matcher.py line 66) ──────────────

class _NullStore:
    async def get(self, _pid):
        return None

    async def find_by_category_severity(self, _c, _s):
        return None


@pytest.mark.asyncio
async def test_matcher_from_batch_labels_not_dict_is_skipped():
    matcher = PlaybookMatcher(_NullStore())
    batch = [
        {"canonical_query_snippet": json.dumps({"labels": "not-a-dict"})},
    ]
    out = await matcher.match_from_batch(batch)
    assert out is None


@pytest.mark.asyncio
async def test_matcher_from_batch_top_level_not_object_is_skipped():
    matcher = PlaybookMatcher(_NullStore())
    batch = [
        # JSON parses to a list — labels lookup must be skipped, not crash.
        {"canonical_query_snippet": json.dumps([{"labels": {"siem_source": "finguard"}}])},
    ]
    out = await matcher.match_from_batch(batch)
    assert out is None


@pytest.mark.asyncio
async def test_matcher_from_batch_iterates_until_finguard_entry():
    """Cover the multi-entry batch path where the first row is skipped."""
    captured: list[tuple[str, str, str]] = []

    class _Recording(_NullStore):
        async def find_by_category_severity(self, c, s):
            captured.append(("find", c, s))
            return _make_pb()

    pb_id = "pb-multi"

    def _make_pb() -> Playbook:
        step = PlaybookStep(
            step_order=1,
            action_type="k8s_rollout_restart",
            target="ns/x",
            params={},
            timeout_sec=30,
            requires_hitl=False,
        )
        return Playbook(
            playbook_id=pb_id, version="1", name="x",
            severity_filter="critical", approved_by="sre",
            steps=(step,), siem_categories=("ddos",),
        )

    matcher = PlaybookMatcher(_Recording())
    batch = [
        {"canonical_query_snippet": "garbage"},  # not JSON → continue
        {"canonical_query_snippet": json.dumps({"no": "labels"})},  # no labels key
        {"canonical_query_snippet": json.dumps({
            "labels": {
                "siem_source": "finguard",
                "siem_category": "ddos",
                "severity": "critical",
            }
        })},
    ]
    out = await matcher.match_from_batch(batch)
    assert out is not None
    assert out.playbook_id == pb_id
    assert captured and captured[0][1:] == ("ddos", "critical")


# ── services.analyst.__main__ — boundary check ────────────────────────────────

def test_analyst_main_prints_boundary_ok():
    """The analyst boundary script must run successfully and emit a status line."""
    from services.analyst import __main__ as analyst_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        analyst_main.main()
    out = buf.getvalue()
    assert "pkg.reasoning OK" in out
    assert "python -m workers" in out


def test_analyst_main_runs_as_script():
    """Exercise the ``python -m services.analyst`` entrypoint via runpy."""
    import runpy

    buf = io.StringIO()
    with redirect_stdout(buf):
        runpy.run_module("services.analyst.__main__", run_name="__main__")
    assert "pkg.reasoning OK" in buf.getvalue()
