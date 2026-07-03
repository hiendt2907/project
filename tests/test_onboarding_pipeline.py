"""Step-3 onboarding pipeline: accumulate discovery evidence, Mermaid generation
(raw text, versioned), ask-loop gap detection, readiness checklist threshold gating.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest

from pkg.onboarding import discovery_doc as dd
from workers import onboarding_pipeline as op


def _redis() -> Any:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


class _FakeAdminRepo:
    def __init__(self) -> None:
        self.chat_ids: dict[str, int] = {}
        self.flags: dict[tuple[str, str], Any] = {}
        self.readiness: dict[str, dict] = {}

    async def get_tenant_telegram_chat_id(self, tenant_id: str) -> int | None:
        return self.chat_ids.get(tenant_id)

    async def get_runtime_flag(self, flag_key: str, tenant_id: str = "default") -> Any | None:
        return self.flags.get((tenant_id, flag_key)) or self.flags.get(("default", flag_key))

    async def set_tenant_readiness(self, *, tenant_id: str, **fields: Any) -> dict[str, Any]:
        self.readiness[tenant_id] = {"tenant_id": tenant_id, **fields}
        return self.readiness[tenant_id]

    async def get_tenant_readiness(self, tenant_id: str) -> dict[str, Any] | None:
        return self.readiness.get(tenant_id)


class _FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_: Any) -> dict[str, Any]:
        self.sent.append((chat_id, text))
        return {"ok": True}


def _ctx(redis: Any, *, admin_repo: Any = None, telegram: Any = None) -> Any:
    return SimpleNamespace(redis=redis, admin_repo=admin_repo, telegram=telegram, settings=SimpleNamespace())


# ── pkg.onboarding.discovery_doc — pure accumulation + mermaid ──────────────

class TestAccumulateAndDiagram:
    @pytest.mark.asyncio
    async def test_accumulate_then_get_doc_round_trips(self):
        r = _redis()
        await dd.accumulate_probe_fact(r, "acme", "process_list", {"processes": [{"name": "nginx", "count": 2}]})
        doc = await dd.get_accumulated_doc(r, "acme")
        assert doc["process_list"]["processes"][0]["name"] == "nginx"

    @pytest.mark.asyncio
    async def test_regenerate_diagrams_creates_new_version_each_call(self):
        r = _redis()
        await dd.accumulate_probe_fact(r, "acme", "service_topology", {"services": [{"name": "api", "status": "running", "description": "REST API"}]})
        v1 = await dd.regenerate_diagrams(r, "acme")
        v2 = await dd.regenerate_diagrams(r, "acme")
        assert v2 == v1 + 1
        text_v1 = await dd.get_diagram_version(r, "acme", v1)
        assert "api" in text_v1
        latest = await dd.get_latest_diagram(r, "acme")
        assert latest is not None
        assert latest[0] == v2

    @pytest.mark.asyncio
    async def test_no_diagram_when_nothing_accumulated_yet(self):
        r = _redis()
        result = await dd.get_latest_diagram(r, "no-such-tenant")
        assert result is None

    def test_mermaid_diagrams_are_raw_text_not_images(self):
        doc = {
            "service_topology": {"services": [{"name": "billing", "status": "running", "description": ""}]},
            "port_scan": {"listening_ports": [{"port": 8080, "service": "billing"}]},
            "process_list": {"processes": [{"name": "billing-worker", "count": 1}]},
        }
        rendered = dd.render_all_diagrams(doc)
        assert rendered.startswith("%%")
        assert "graph TD" in rendered
        assert "sequenceDiagram" in rendered
        assert "flowchart LR" in rendered


# ── readiness checklist — threshold-as-config, never hardcoded in caller ───

class TestReadinessThresholds:
    @pytest.mark.asyncio
    async def test_default_thresholds_used_when_no_admin_repo(self):
        thresholds = await dd.resolve_readiness_thresholds(None, "acme")
        assert thresholds == dd.DEFAULT_READINESS_THRESHOLDS

    @pytest.mark.asyncio
    async def test_per_tenant_override_takes_precedence_over_global_default(self):
        repo = _FakeAdminRepo()
        repo.flags[("default", "readiness_threshold:default")] = {"endpoint_mapped_pct_min": 50.0}
        repo.flags[("acme", "readiness_threshold:acme")] = {"endpoint_mapped_pct_min": 10.0}
        thresholds = await dd.resolve_readiness_thresholds(repo, "acme")
        assert thresholds["endpoint_mapped_pct_min"] == 10.0

    @pytest.mark.asyncio
    async def test_readiness_flag_false_until_all_three_thresholds_cross(self):
        r = _redis()
        repo = _FakeAdminRepo()
        # endpoint_mapped 100%, business_flow 0% → not ready
        await dd.accumulate_probe_fact(r, "acme", "port_scan", {"listening_ports": [{"port": 80, "service": "http"}]})
        fields = await dd.compute_readiness(r, repo, "acme")
        assert fields["endpoint_mapped_pct"] == 100.0
        assert fields["business_flow_confirmed_pct"] == 0.0
        assert fields["readiness_flag"] is False

    @pytest.mark.asyncio
    async def test_readiness_flag_true_when_all_three_thresholds_cross(self):
        r = _redis()
        repo = _FakeAdminRepo()
        await dd.accumulate_probe_fact(r, "acme", "port_scan", {"listening_ports": [{"port": 80, "service": "http"}]})
        await dd.accumulate_probe_fact(r, "acme", "service_topology", {"services": [{"name": "api", "status": "running", "description": "REST API"}]})
        fields = await dd.compute_readiness(r, repo, "acme")
        assert fields["endpoint_mapped_pct"] == 100.0
        assert fields["business_flow_confirmed_pct"] == 100.0
        assert fields["open_questions_over_threshold"] == 0
        assert fields["readiness_flag"] is True

    @pytest.mark.asyncio
    async def test_answered_human_claim_counts_toward_business_flow_pct(self):
        """Closes the iteration-15 gap: a service with no discovery-doc description
        but an answered Human Claim (competency_matrix business_capability=CLAIMED)
        must still count as confirmed — answering Questions now moves readiness."""
        import time as _time

        from aoip.claims_store import ClaimRecord, put_claim

        r = _redis()
        repo = _FakeAdminRepo()
        await dd.accumulate_probe_fact(r, "acme", "port_scan", {"listening_ports": [{"port": 80, "service": "http"}]})
        await dd.accumulate_probe_fact(
            r, "acme", "service_topology", {"services": [{"name": "api", "status": "running"}]},
        )
        fields_before = await dd.compute_readiness(r, repo, "acme")
        assert fields_before["business_flow_confirmed_pct"] == 0.0

        await put_claim(r, "acme", ClaimRecord(
            subject="svc:api", predicate="serves_capability", value="checkout",
            answered_by="human:iter17-productizer", answered_at=_time.time(), question_id="q-iter17",
        ))
        fields_after = await dd.compute_readiness(r, repo, "acme")
        assert fields_after["business_flow_confirmed_pct"] == 100.0

    @pytest.mark.asyncio
    async def test_stale_open_question_blocks_readiness_even_if_pct_ok(self):
        r = _redis()
        repo = _FakeAdminRepo()
        repo.flags[("default", "readiness_threshold:default")] = {"open_question_stale_days": 0}
        await dd.accumulate_probe_fact(r, "acme", "port_scan", {"listening_ports": [{"port": 80, "service": "http"}]})
        await dd.accumulate_probe_fact(r, "acme", "service_topology", {"services": [{"name": "api", "status": "running", "description": "REST API"}]})
        await r.zadd(dd.QUESTIONS_OPEN_KEY.format(tenant_id="acme"), {"q1": 0})  # ancient, always stale
        fields = await dd.compute_readiness(r, repo, "acme")
        assert fields["open_questions_over_threshold"] == 1
        assert fields["readiness_flag"] is False


# ── workers.onboarding_pipeline — Kafka/ctx orchestration ───────────────────

class TestAccumulateDiscoveryEvidence:
    @pytest.mark.asyncio
    async def test_accumulates_and_regenerates_diagram(self):
        r = _redis()
        ctx = _ctx(r)
        ev_doc = {
            "tenant_id": "acme", "probe": "process_list", "trace_id": "tr-1",
            "extracted_fact": {"discovery_data": {"processes": [{"name": "nginx", "count": 3}]}},
        }
        await op.accumulate_discovery_evidence(ctx, ev_doc)
        doc = await dd.get_accumulated_doc(r, "acme")
        assert doc["process_list"]["processes"][0]["name"] == "nginx"
        assert await dd.get_latest_diagram(r, "acme") is not None

    @pytest.mark.asyncio
    async def test_missing_discovery_data_is_a_noop_not_a_crash(self):
        r = _redis()
        ctx = _ctx(r)
        await op.accumulate_discovery_evidence(ctx, {"tenant_id": "acme", "probe": "process_list", "extracted_fact": {}})
        doc = await dd.get_accumulated_doc(r, "acme")
        assert doc == {}

    @pytest.mark.asyncio
    async def test_no_admin_repo_skips_readiness_silently(self):
        r = _redis()
        ctx = _ctx(r, admin_repo=None)
        ev_doc = {
            "tenant_id": "acme", "probe": "process_list",
            "extracted_fact": {"discovery_data": {"processes": [{"name": "nginx", "count": 1}]}},
        }
        await op.accumulate_discovery_evidence(ctx, ev_doc)  # must not raise


class TestSystemModelDualWrite:
    """Slice O1: additive AOIP Fact/SystemModel projection alongside the legacy
    flat-doc write. Must never lose the legacy write, never raise, never claim
    success on failure."""

    @pytest.mark.asyncio
    async def test_discovery_evidence_also_folds_into_system_model(self):
        from aoip.system_model_store import load_system_model

        r = _redis()
        ctx = _ctx(r)
        ev_doc = {
            "tenant_id": "acme", "agent_id": "agent-1", "namespace": "web-01",
            "probe": "port_scan", "trace_id": "tr-1",
            "extracted_fact": {"discovery_data": {"listening_ports": [{"port": 80, "service": "nginx"}]}},
        }
        await op.accumulate_discovery_evidence(ctx, ev_doc)
        model, revision = await load_system_model(r, "acme")
        assert revision == 1
        assert ("host:web-01", "runs_service", "nginx") in {f.triple for f in model.facts}
        # legacy path is untouched by the additive write
        doc = await dd.get_accumulated_doc(r, "acme")
        assert doc["port_scan"]["listening_ports"][0]["port"] == 80

    @pytest.mark.asyncio
    async def test_projection_failure_never_loses_legacy_write(self, monkeypatch):
        import aoip.onboarding_projection as proj

        def _boom(*a, **k):
            raise RuntimeError("projection exploded")

        monkeypatch.setattr(proj, "to_observation", _boom)

        r = _redis()
        ctx = _ctx(r)
        ev_doc = {
            "tenant_id": "acme", "probe": "process_list", "trace_id": "tr-2",
            "extracted_fact": {"discovery_data": {"processes": [{"name": "nginx", "count": 1}]}},
        }
        await op.accumulate_discovery_evidence(ctx, ev_doc)  # must not raise
        doc = await dd.get_accumulated_doc(r, "acme")
        assert doc["process_list"]["processes"][0]["name"] == "nginx"

    @pytest.mark.asyncio
    async def test_provenance_uses_real_agent_id_from_nested_extracted_fact(self):
        """Real gateway envelopes (agent_webhook.py) nest agent_id/hostname
        INSIDE extracted_fact, never at ev_doc top level. A top-level-only
        lookup silently falls back to "unknown", weakening Fact provenance
        for every projected Fact without ever raising or failing loudly."""
        from aoip.system_model_store import load_system_model

        r = _redis()
        ctx = _ctx(r)
        ev_doc = {
            "tenant_id": "acme", "namespace": "web-02",
            "probe": "port_scan", "trace_id": "tr-3",
            "extracted_fact": {
                "agent_id": "staging-sim_cust-app", "hostname": "cust-app",
                "discovery_data": {"listening_ports": [{"port": 8080, "service": ""}]},
            },
        }
        await op.accumulate_discovery_evidence(ctx, ev_doc)
        model, _revision = await load_system_model(r, "acme")
        fact = next(f for f in model.facts if f.triple == ("host:web-02", "exposes_port", "8080"))
        assert "agent:staging-sim_cust-app" in fact.provenance
        assert "agent:unknown" not in fact.provenance

    @pytest.mark.asyncio
    async def test_provenance_survives_coerce_evidence_dict_realistic_size(self):
        """End-to-end through coerce_evidence_dict() (pkg/reasoning/schema.py),
        exactly like kafka_discovery_evidence_loop's real caller
        (evidence_consumer.py) does, with a realistic-size payload (matches a
        live-captured cust-db process_list envelope, well under the 2000-char
        extracted_fact truncation cap)."""
        from pkg.reasoning import coerce_evidence_dict

        from aoip.system_model_store import load_system_model

        r = _redis()
        ctx = _ctx(r)
        raw_envelope = {
            "tenant_id": "acme", "namespace": "cust-app",
            "probe": "process_list", "trace_id": "tr-4",
            "extracted_fact": {
                "discovery_data": {"processes": [{"name": "nginx", "count": 1}]},
                "agent_id": "staging-sim_cust-app", "hostname": "cust-app",
            },
        }
        ev_doc = coerce_evidence_dict(raw_envelope)
        await op.accumulate_discovery_evidence(ctx, ev_doc)
        model, _revision = await load_system_model(r, "acme")
        assert any(f.subject == "host:cust-app" for f in model.facts)
        sample = next(f for f in model.facts if f.subject == "host:cust-app")
        assert "agent:staging-sim_cust-app" in sample.provenance
        assert "agent:unknown" not in sample.provenance


class TestTwoAgentsTwoTenantsOneVM:
    """Regression coverage for the scenario proven live-only in iteration 9
    (docs/product/PRODUCT_PROOF.md): two Remote Agent instances bound to two
    different tenants, running on the SAME physical VM (identical hostname),
    both feeding discovery evidence through the real gateway->worker pipeline.
    Each tenant's Twin must stay fully isolated even though both Agents
    observe the same host."""

    @pytest.mark.asyncio
    async def test_same_hostname_two_tenants_twins_stay_isolated(self):
        from aoip.system_model_store import load_system_model

        r = _redis()
        ctx = _ctx(r)
        ev_staging_sim = {
            "tenant_id": "staging-sim", "agent_id": "staging-sim_cust-edge",
            "namespace": "cust-edge", "probe": "port_scan", "trace_id": "tr-staging-sim",
            "extracted_fact": {"discovery_data": {"listening_ports": [{"port": 80, "service": "nginx"}]}},
        }
        ev_replay01 = {
            "tenant_id": "tenant-replay-01", "agent_id": "replay01_cust-edge",
            "namespace": "cust-edge", "probe": "port_scan", "trace_id": "tr-replay01",
            "extracted_fact": {"discovery_data": {"listening_ports": [{"port": 443, "service": "envoy"}]}},
        }
        await op.accumulate_discovery_evidence(ctx, ev_staging_sim)
        await op.accumulate_discovery_evidence(ctx, ev_replay01)

        staging_model, staging_rev = await load_system_model(r, "staging-sim")
        replay_model, replay_rev = await load_system_model(r, "tenant-replay-01")

        assert staging_rev == 1
        assert replay_rev == 1
        staging_triples = {f.triple for f in staging_model.facts}
        replay_triples = {f.triple for f in replay_model.facts}
        assert ("host:cust-edge", "runs_service", "nginx") in staging_triples
        assert ("host:cust-edge", "exposes_port", "80") in staging_triples
        assert ("host:cust-edge", "runs_service", "envoy") not in staging_triples
        assert ("host:cust-edge", "exposes_port", "443") not in staging_triples
        assert ("host:cust-edge", "runs_service", "envoy") in replay_triples
        assert ("host:cust-edge", "exposes_port", "443") in replay_triples
        assert ("host:cust-edge", "runs_service", "nginx") not in replay_triples

        # legacy flat-doc accumulation (pkg.onboarding.discovery_doc) is also
        # per-tenant keyed — cross-check it stays isolated too.
        staging_doc = await dd.get_accumulated_doc(r, "staging-sim")
        replay_doc = await dd.get_accumulated_doc(r, "tenant-replay-01")
        assert staging_doc["port_scan"]["listening_ports"][0]["service"] == "nginx"
        assert replay_doc["port_scan"]["listening_ports"][0]["service"] == "envoy"

    @pytest.mark.asyncio
    async def test_same_hostname_two_tenants_provenance_never_cross_tags(self):
        from aoip.system_model_store import load_system_model

        r = _redis()
        ctx = _ctx(r)
        await op.accumulate_discovery_evidence(ctx, {
            "tenant_id": "staging-sim", "agent_id": "staging-sim_cust-edge",
            "namespace": "cust-edge", "probe": "process_list", "trace_id": "tr-a",
            "extracted_fact": {"discovery_data": {"processes": [{"name": "nginx", "count": 1}]}},
        })
        await op.accumulate_discovery_evidence(ctx, {
            "tenant_id": "tenant-replay-01", "agent_id": "replay01_cust-edge",
            "namespace": "cust-edge", "probe": "process_list", "trace_id": "tr-b",
            "extracted_fact": {"discovery_data": {"processes": [{"name": "envoy", "count": 1}]}},
        })

        staging_model, _ = await load_system_model(r, "staging-sim")
        replay_model, _ = await load_system_model(r, "tenant-replay-01")
        staging_fact = next(f for f in staging_model.facts if f.subject == "host:cust-edge")
        replay_fact = next(f for f in replay_model.facts if f.subject == "host:cust-edge")
        assert "agent:staging-sim_cust-edge" in staging_fact.provenance
        assert "agent:replay01_cust-edge" not in staging_fact.provenance
        assert "agent:replay01_cust-edge" in replay_fact.provenance
        assert "agent:staging-sim_cust-edge" not in replay_fact.provenance


class TestOneTenantTwoHosts:
    """Regression coverage for iteration 14 (docs/product/PRODUCT_PROOF.md): a
    single tenant's Twin must accumulate and merge facts from multiple distinct
    hosts (proven live by installing a second Remote Agent for tenant-replay-01
    on VM cust-app, alongside its existing agent on cust-edge)."""

    @pytest.mark.asyncio
    async def test_single_tenant_twin_merges_facts_from_two_distinct_hosts(self):
        from aoip.system_model_store import load_system_model

        r = _redis()
        ctx = _ctx(r)
        await op.accumulate_discovery_evidence(ctx, {
            "tenant_id": "tenant-replay-01", "agent_id": "tenant-replay-01_cust-edge",
            "namespace": "cust-edge", "probe": "port_scan", "trace_id": "tr-edge",
            "extracted_fact": {"discovery_data": {"listening_ports": [{"port": 443, "service": "envoy"}]}},
        })
        await op.accumulate_discovery_evidence(ctx, {
            "tenant_id": "tenant-replay-01", "agent_id": "tenant-replay-01_cust-app",
            "namespace": "cust-app", "probe": "port_scan", "trace_id": "tr-app",
            "extracted_fact": {"discovery_data": {"listening_ports": [{"port": 8080, "service": "app"}]}},
        })

        model, revision = await load_system_model(r, "tenant-replay-01")

        assert revision == 2
        triples = {f.triple for f in model.facts}
        assert ("host:cust-edge", "runs_service", "envoy") in triples
        assert ("host:cust-edge", "exposes_port", "443") in triples
        assert ("host:cust-app", "runs_service", "app") in triples
        assert ("host:cust-app", "exposes_port", "8080") in triples
        hosts = {f.subject for f in model.facts if f.subject.startswith("host:")}
        assert hosts == {"host:cust-edge", "host:cust-app"}

    @pytest.mark.asyncio
    async def test_second_host_provenance_tags_its_own_agent_not_the_first(self):
        from aoip.system_model_store import load_system_model

        r = _redis()
        ctx = _ctx(r)
        await op.accumulate_discovery_evidence(ctx, {
            "tenant_id": "tenant-replay-01", "agent_id": "tenant-replay-01_cust-edge",
            "namespace": "cust-edge", "probe": "process_list", "trace_id": "tr-edge",
            "extracted_fact": {"discovery_data": {"processes": [{"name": "nginx", "count": 1}]}},
        })
        await op.accumulate_discovery_evidence(ctx, {
            "tenant_id": "tenant-replay-01", "agent_id": "tenant-replay-01_cust-app",
            "namespace": "cust-app", "probe": "process_list", "trace_id": "tr-app",
            "extracted_fact": {"discovery_data": {"processes": [{"name": "gunicorn", "count": 1}]}},
        })

        model, _ = await load_system_model(r, "tenant-replay-01")
        edge_fact = next(f for f in model.facts if f.subject == "host:cust-edge")
        app_fact = next(f for f in model.facts if f.subject == "host:cust-app")
        assert "agent:tenant-replay-01_cust-edge" in edge_fact.provenance
        assert "agent:tenant-replay-01_cust-app" not in edge_fact.provenance
        assert "agent:tenant-replay-01_cust-app" in app_fact.provenance
        assert "agent:tenant-replay-01_cust-edge" not in app_fact.provenance


class TestCoerceEvidenceDictAgentIdPromotion:
    """coerce_evidence_dict() (pkg/reasoning/schema.py) promotes agent_id/hostname
    to top-level fields BEFORE truncating extracted_fact to 2000 chars — otherwise
    a large discovery_data payload silently drops them (they're appended LAST by
    the gateway's dict-spread in agent_webhook.py). This class tests that
    boundary directly; TestSystemModelDualWrite covers the realistic-size,
    full-pipeline case."""

    def test_agent_id_hostname_promoted_even_when_extracted_fact_would_truncate(self):
        from pkg.reasoning import coerce_evidence_dict

        big_processes = [{"name": f"worker-proc-{i}"} for i in range(200)]
        raw_envelope = {
            "probe": "process_list", "trace_id": "tr-5",
            "extracted_fact": {
                "discovery_data": {"processes": big_processes},
                "agent_id": "staging-sim_cust-app", "hostname": "cust-app",
            },
        }
        assert len(json.dumps(raw_envelope["extracted_fact"])) > 2000  # sanity: fixture is actually large
        out = coerce_evidence_dict(raw_envelope)
        assert out.get("agent_id") == "staging-sim_cust-app"
        assert out.get("hostname") == "cust-app"
        # extracted_fact itself IS truncated/possibly-invalid-JSON at this size —
        # that's a separate, pre-existing risk (not this fix's scope); this test
        # only asserts the promoted identity fields survive regardless.

    def test_no_agent_id_field_is_a_noop(self):
        from pkg.reasoning import coerce_evidence_dict

        out = coerce_evidence_dict({"probe": "sys_metric", "extracted_fact": {"cpu": 1}})
        assert "agent_id" not in out
        assert "hostname" not in out


class TestAskLoop:
    @pytest.mark.asyncio
    async def test_unnamed_service_gap_opens_question_and_sends_telegram(self):
        r = _redis()
        repo = _FakeAdminRepo()
        repo.chat_ids["acme"] = 555
        telegram = _FakeTelegram()
        ctx = _ctx(r, admin_repo=repo, telegram=telegram)
        ev_doc = {
            "tenant_id": "acme", "probe": "service_topology", "trace_id": "tr-2",
            "extracted_fact": {"discovery_data": {"services": [{"name": "mystery-svc", "status": "running", "description": ""}]}},
        }
        await op.accumulate_discovery_evidence(ctx, ev_doc)
        assert len(telegram.sent) == 1
        assert telegram.sent[0][0] == 555
        open_count = await r.zcard(dd.QUESTIONS_OPEN_KEY.format(tenant_id="acme"))
        assert open_count == 1

    @pytest.mark.asyncio
    async def test_two_tenants_question_routed_to_own_chat_id_only(self):
        r = _redis()
        repo = _FakeAdminRepo()
        repo.chat_ids["acme"] = 111
        repo.chat_ids["globex"] = 222
        telegram = _FakeTelegram()
        ctx = _ctx(r, admin_repo=repo, telegram=telegram)

        await op.accumulate_discovery_evidence(ctx, {
            "tenant_id": "acme", "probe": "service_topology", "trace_id": "tr-a",
            "extracted_fact": {"discovery_data": {"services": [{"name": "svc-a", "status": "running", "description": ""}]}},
        })
        await op.accumulate_discovery_evidence(ctx, {
            "tenant_id": "globex", "probe": "service_topology", "trace_id": "tr-g",
            "extracted_fact": {"discovery_data": {"services": [{"name": "svc-g", "status": "running", "description": ""}]}},
        })

        assert len(telegram.sent) == 2
        sent_chat_ids = {chat_id for chat_id, _ in telegram.sent}
        assert sent_chat_ids == {111, 222}

        acme_open = await r.zcard(dd.QUESTIONS_OPEN_KEY.format(tenant_id="acme"))
        globex_open = await r.zcard(dd.QUESTIONS_OPEN_KEY.format(tenant_id="globex"))
        assert acme_open == 1
        assert globex_open == 1

    @pytest.mark.asyncio
    async def test_no_chat_id_configured_skips_telegram_send_without_error(self):
        r = _redis()
        repo = _FakeAdminRepo()  # no chat_id registered
        telegram = _FakeTelegram()
        ctx = _ctx(r, admin_repo=repo, telegram=telegram)
        ev_doc = {
            "tenant_id": "acme", "probe": "port_scan",
            "extracted_fact": {"discovery_data": {"listening_ports": [{"port": 9999, "service": ""}]}},
        }
        await op.accumulate_discovery_evidence(ctx, ev_doc)
        assert telegram.sent == []

    @pytest.mark.asyncio
    async def test_resolve_question_removes_from_open_set(self):
        r = _redis()
        ctx = _ctx(r)
        qid = await op._open_question(ctx, "acme", "test question")
        assert await r.zcard(dd.QUESTIONS_OPEN_KEY.format(tenant_id="acme")) == 1
        resolved = await op.resolve_question(ctx, "acme", qid)
        assert resolved is True
        assert await r.zcard(dd.QUESTIONS_OPEN_KEY.format(tenant_id="acme")) == 0

    @pytest.mark.asyncio
    async def test_resolve_unknown_question_id_returns_false(self):
        r = _redis()
        ctx = _ctx(r)
        assert await op.resolve_question(ctx, "acme", "nope") is False


class TestRecomputeReadiness:
    @pytest.mark.asyncio
    async def test_persists_via_admin_repo_set_tenant_readiness(self):
        r = _redis()
        repo = _FakeAdminRepo()
        ctx = _ctx(r, admin_repo=repo)
        await dd.accumulate_probe_fact(r, "acme", "port_scan", {"listening_ports": [{"port": 80, "service": "http"}]})
        result = await op.recompute_readiness(ctx, "acme")
        assert result is not None
        assert repo.readiness["acme"]["endpoint_mapped_pct"] == 100.0


class TestHandoverDocUpload:
    @pytest.mark.asyncio
    async def test_handover_doc_feeds_same_accumulation_pipeline(self):
        r = _redis()
        repo = _FakeAdminRepo()
        ctx = _ctx(r, admin_repo=repo)
        await op.accumulate_handover_document(ctx, "acme", filename="README.md", content="# Acme service")
        doc = await dd.get_accumulated_doc(r, "acme")
        assert doc["doc_snapshot"]["documents"][0]["path"] == "README.md"
        assert await dd.get_latest_diagram(r, "acme") is not None

    @pytest.mark.asyncio
    async def test_handover_doc_content_never_persisted_on_omni_side(self):
        """Data residency: docs/knowledge/handover stay on the customer's system —
        Omni only stores a hash + length reference, never the text itself."""
        r = _redis()
        ctx = _ctx(r)
        await op.accumulate_handover_document(ctx, "acme", filename="secrets.md", content="internal runbook details")
        doc = await dd.get_accumulated_doc(r, "acme")
        stored = doc["doc_snapshot"]["documents"][0]
        assert "content" not in stored
        assert stored["content_hash"] == hashlib.sha256(b"internal runbook details").hexdigest()
        assert stored["content_length"] == len("internal runbook details")

    @pytest.mark.asyncio
    async def test_service_description_text_never_persisted_only_described_flag(self):
        """Data residency: tenant-authored business-purpose text stays local —
        Omni only keeps a described/not-described mapping per service."""
        r = _redis()
        await dd.accumulate_probe_fact(
            r, "acme", "service_topology",
            {"services": [{"name": "billing", "status": "running", "description": "handles invoices"}]},
        )
        doc = await dd.get_accumulated_doc(r, "acme")
        stored = doc["service_topology"]["services"][0]
        assert "description" not in stored
        assert stored["described"] is True
        assert stored["name"] == "billing"
