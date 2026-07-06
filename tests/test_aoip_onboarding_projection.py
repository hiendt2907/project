"""Slice O1: DiscoveryEvidence envelope -> Observation -> Fact projection."""
from __future__ import annotations

import json

import fakeredis
import pytest

from aoip.onboarding_projection import project_facts, resolve_ip_to_host_map, to_observation


def _envelope(probe: str, discovery_data: dict, *, trace_id: str = "tr-1") -> dict:
    return {
        "probe": probe,
        "trace_id": trace_id,
        "extracted_fact": {"discovery_data": discovery_data},
    }


class TestToObservation:
    def test_process_list_produces_observation(self):
        ev = _envelope("process_list", {"processes": [{"name": "nginx", "count": 2}]})
        obs = to_observation(ev, tenant_id="acme", agent_id="agent-1", host="web-01")
        assert obs is not None
        assert obs.source == "discovery:process_list"
        assert obs.scope == "acme/web-01"
        assert obs.data["tenant_id"] == "acme"
        assert obs.data["agent_id"] == "agent-1"
        assert obs.data["schema_version"] == 1
        assert "content_hash" in obs.data

    def test_unsupported_probe_returns_none(self):
        ev = _envelope("cpu_starvation", {"foo": "bar"})
        assert to_observation(ev, tenant_id="acme", agent_id="a", host="h") is None

    def test_malformed_evidence_returns_none(self):
        ev = {"probe": "process_list", "extracted_fact": {}}
        assert to_observation(ev, tenant_id="acme", agent_id="a", host="h") is None

    def test_deterministic_content_hash_same_payload(self):
        ev1 = _envelope("port_scan", {"listening_ports": [{"port": 80, "service": "nginx"}]})
        ev2 = _envelope("port_scan", {"listening_ports": [{"port": 80, "service": "nginx"}]}, trace_id="tr-2")
        o1 = to_observation(ev1, tenant_id="acme", agent_id="a", host="h")
        o2 = to_observation(ev2, tenant_id="acme", agent_id="a", host="h")
        assert o1.data["content_hash"] == o2.data["content_hash"]


class TestProjectFacts:
    def test_process_list_projects_runs_process_facts(self):
        ev = _envelope("process_list", {"processes": [{"name": "nginx", "count": 2}]})
        obs = to_observation(ev, tenant_id="acme", agent_id="a1", host="web-01")
        facts = project_facts(obs)
        assert len(facts) == 1
        f = facts[0]
        assert f.subject == "host:web-01"
        assert f.predicate == "runs_process"
        assert f.obj == "nginx"
        assert any(p.startswith("discovery:process_list") for p in f.provenance)

    def test_port_scan_projects_port_and_service_facts(self):
        ev = _envelope("port_scan", {"listening_ports": [{"port": 6379, "service": "redis"}]})
        obs = to_observation(ev, tenant_id="acme", agent_id="a1", host="db-01")
        facts = project_facts(obs)
        predicates = {(f.predicate, f.obj) for f in facts}
        assert ("exposes_port", "6379") in predicates
        assert ("runs_service", "redis") in predicates

    def test_service_topology_projects_runs_service_no_description_text(self):
        ev = _envelope(
            "service_topology",
            {"services": [{"name": "billing", "status": "running", "description": "handles invoices"}]},
        )
        obs = to_observation(ev, tenant_id="acme", agent_id="a1", host="app-01")
        facts = project_facts(obs)
        assert len(facts) == 1
        assert facts[0].obj == "billing"
        # INV_DATA_RESIDENCY: narrative description text must never leak into a Fact
        assert all("handles invoices" not in v for v in (facts[0].subject, facts[0].obj))

    def test_doc_snapshot_projects_hash_reference_not_raw_content(self):
        ev = _envelope("doc_snapshot", {"documents": [{"path": "README.md", "content": "SECRET internals"}]})
        obs = to_observation(ev, tenant_id="acme", agent_id="a1", host="app-01")
        facts = project_facts(obs)
        assert len(facts) == 1
        f = facts[0]
        assert f.predicate == "observed_from"
        assert f.obj == "host:app-01"
        assert "SECRET internals" not in f.subject
        assert "SECRET internals" not in f.obj

    def test_empty_discovery_data_yields_no_facts(self):
        ev = _envelope("process_list", {"processes": []})
        obs = to_observation(ev, tenant_id="acme", agent_id="a1", host="h")
        assert project_facts(obs) == ()

    def test_connection_scan_projects_connects_to_when_ip_resolves(self):
        ev = _envelope(
            "connection_scan",
            {"connections": [{"local_port": 44444, "remote_ip": "10.0.0.9", "remote_port": 6379, "process": "app"}]},
        )
        obs = to_observation(ev, tenant_id="acme", agent_id="a1", host="web-01")
        facts = project_facts(obs, ip_to_host={"10.0.0.9": "db-01"})
        assert len(facts) == 1
        f = facts[0]
        assert f.subject == "host:web-01"
        assert f.predicate == "connects_to"
        assert f.obj == "host:db-01"

    def test_connection_scan_yields_no_fact_when_ip_unresolved(self):
        """External peer (Internet/DNS/NTP) with no matching agent must not produce a fact."""
        ev = _envelope(
            "connection_scan",
            {"connections": [{"local_port": 44444, "remote_ip": "8.8.8.8", "remote_port": 443, "process": "curl"}]},
        )
        obs = to_observation(ev, tenant_id="acme", agent_id="a1", host="web-01")
        assert project_facts(obs, ip_to_host={"10.0.0.9": "db-01"}) == ()
        assert project_facts(obs, ip_to_host=None) == ()

    def test_connection_scan_self_connection_yields_no_fact(self):
        ev = _envelope(
            "connection_scan",
            {"connections": [{"local_port": 44444, "remote_ip": "10.0.0.5", "remote_port": 6379, "process": "app"}]},
        )
        obs = to_observation(ev, tenant_id="acme", agent_id="a1", host="web-01")
        assert project_facts(obs, ip_to_host={"10.0.0.5": "web-01"}) == ()


class TestResolveIpToHostMap:
    @pytest.mark.asyncio
    async def test_maps_ip_to_hostname_for_same_tenant(self):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set(
            "omni:remote_agent:registry:agent-1",
            json.dumps({"agent_id": "agent-1", "hostname": "db-01", "tenant_id": "acme", "remote_ip": "10.0.0.9"}),
        )
        await redis.set(
            "omni:remote_agent:registry:agent-2",
            json.dumps({"agent_id": "agent-2", "hostname": "other-01", "tenant_id": "other-tenant", "remote_ip": "10.0.0.7"}),
        )
        mapping = await resolve_ip_to_host_map(redis, "acme")
        assert mapping == {"10.0.0.9": "db-01"}

    @pytest.mark.asyncio
    async def test_missing_remote_ip_is_skipped(self):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set(
            "omni:remote_agent:registry:agent-1",
            json.dumps({"agent_id": "agent-1", "hostname": "db-01", "tenant_id": "acme"}),
        )
        mapping = await resolve_ip_to_host_map(redis, "acme")
        assert mapping == {}
