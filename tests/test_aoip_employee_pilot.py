"""Sprint NV-SRE IT-4 — AOIP employee entrypoint (pilot migration cust-app).

Chiến lược parity (docs/plans/it4-collector-parity-checklist.md): MỘT process,
HAI vòng song song — telemetry reuse ``remote_agent.agent.run_agent()`` nguyên
vẹn (collectors đã battle-tested) + durable command daemon của aoip (ADR-001).

Drift (IT-2) mở rộng: employee ship thêm package ``aoip`` lên VM → tự hash và
báo ``aoip_bundle_sha256`` khi register; manifest release publish cả hai hash.
Agent legacy (2 VM còn lại) chỉ báo hash cũ → KHÔNG bị đánh drifted oan.

E2E style theo tests/test_remote_agent_e2e.py: real OmniEmitter ↔ real gateway
routers qua ASGITransport, không mock business logic hai đầu dây.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI

from remote_agent import emitter as emitter_mod
from remote_agent.bundle_hash import compute_bundle_hash
from remote_agent.emitter import OmniEmitter


def _build_app() -> FastAPI:
    from gateway.routes.agent_commands import router as commands_router
    from gateway.routes.agent_webhook import router as webhook_router

    app = FastAPI()
    app.state.redis = FakeRedis(decode_responses=True)
    app.state.kafka = AsyncMock()
    app.state.kafka_topic_evidence = "omni-diagnostic-evidence"
    app.include_router(webhook_router)
    app.include_router(commands_router)
    return app


def _make_emitter(monkeypatch, app: FastAPI, agent_id: str = "agent-emp-1") -> OmniEmitter:
    monkeypatch.setattr(emitter_mod, "_make_transport", lambda: httpx.ASGITransport(app=app))
    return OmniEmitter(
        gateway_url="http://test",
        api_key="test-key",
        agent_id=agent_id,
        hostname="employee-host",
        tenant_id="default",
    )


def _make_request(redis: Any) -> MagicMock:
    req = MagicMock()
    req.app.state.redis = redis
    return req


# ── self-hash package aoip: cùng thuật toán với publisher (so sánh được) ────────

class TestAoipSelfBundleHash:
    def test_matches_publisher_algorithm_over_repo_package(self):
        import aoip
        from aoip.agent.employee import aoip_self_bundle_hash

        expected = compute_bundle_hash(Path(aoip.__file__).resolve().parent)
        h = aoip_self_bundle_hash()
        assert h == expected
        assert len(h) == 64
        int(h, 16)


class TestPublisherManifestCoversAoip:
    def test_manifest_contains_both_hashes(self):
        from scripts.publish_agent_release import REPO_ROOT, build_manifest, build_release_tar

        manifest = build_manifest(build_release_tar())  # IT-5: manifest cần tarball
        assert manifest["bundle_sha256"] == compute_bundle_hash(
            REPO_ROOT / "src" / "remote_agent")
        assert manifest["aoip_bundle_sha256"] == compute_bundle_hash(
            REPO_ROOT / "src" / "aoip")


# ── register mang aoip_bundle_sha256 qua wire thật → registry record ───────────

class TestRegisterCarriesAoipHash:
    @pytest.mark.asyncio
    async def test_extra_register_fields_stored_in_registry(self, monkeypatch):
        app = _build_app()
        agent = _make_emitter(monkeypatch, app)

        thresholds = await agent.register(
            capabilities=["metrics"],
            bundle_sha256="a" * 64,
            extra_fields={"aoip_bundle_sha256": "c" * 64},
        )

        assert thresholds is not None  # round-trip thành công
        raw = await app.state.redis.get("omni:remote_agent:registry:agent-emp-1")
        rec = json.loads(raw)
        assert rec["bundle_sha256"] == "a" * 64
        assert rec["aoip_bundle_sha256"] == "c" * 64

    @pytest.mark.asyncio
    async def test_legacy_register_without_extra_fields_unchanged(self, monkeypatch):
        app = _build_app()
        agent = _make_emitter(monkeypatch, app, agent_id="agent-legacy-1")

        await agent.register(capabilities=["metrics"], bundle_sha256="a" * 64)

        raw = await app.state.redis.get("omni:remote_agent:registry:agent-legacy-1")
        rec = json.loads(raw)
        assert rec["bundle_sha256"] == "a" * 64
        assert rec.get("aoip_bundle_sha256", "") == ""

    @pytest.mark.asyncio
    async def test_run_agent_threads_extra_register_fields(self, monkeypatch):
        """run_agent() phải chuyển extra_register_fields tới emitter.register."""
        from remote_agent.agent import run_agent

        monkeypatch.setenv("OMNI_AGENT_GATEWAY_URL", "http://test")
        monkeypatch.setenv("OMNI_AGENT_API_KEY", "test-key")

        captured: dict = {}

        class _Stop(Exception):
            pass

        async def fake_register(self, capabilities, version="1.0.0",
                                k8s_namespace="", bundle_sha256="",
                                extra_fields=None):
            captured["extra_fields"] = extra_fields
            raise _Stop  # dừng loop ngay sau register đầu tiên

        async def fake_discovery(agent_id, hostname):
            raise RuntimeError("discovery skipped in test")  # nhánh non-fatal thật

        monkeypatch.setattr("remote_agent.discovery.run_vm_discovery", fake_discovery)
        monkeypatch.setattr(OmniEmitter, "register", fake_register)

        with pytest.raises(_Stop):
            await run_agent(extra_register_fields={"aoip_bundle_sha256": "c" * 64})
        assert captured["extra_fields"] == {"aoip_bundle_sha256": "c" * 64}


# ── drift classification contract mở rộng (giữ nguyên contract IT-2 cho legacy) ─

_MANIFEST_BOTH = {
    "version": "1.2.0",
    "bundle_sha256": "a" * 64,
    "aoip_bundle_sha256": "c" * 64,
    "published_at": 1,
}
_MANIFEST_LEGACY = {"version": "1.2.0", "bundle_sha256": "a" * 64, "published_at": 1}


class TestClassifyDriftWithAoipHash:
    def test_legacy_agent_judged_on_remote_agent_hash_only(self):
        from gateway.routes.agent_commands import _classify_drift

        rec = {"version": "1.2.0", "bundle_sha256": "a" * 64}
        assert _classify_drift(rec, _MANIFEST_BOTH) == "current"

    def test_employee_matching_both_hashes_is_current(self):
        from gateway.routes.agent_commands import _classify_drift

        rec = {"version": "1.2.0", "bundle_sha256": "a" * 64,
               "aoip_bundle_sha256": "c" * 64}
        assert _classify_drift(rec, _MANIFEST_BOTH) == "current"

    def test_employee_aoip_hash_mismatch_is_drifted(self):
        from gateway.routes.agent_commands import _classify_drift

        rec = {"version": "1.2.0", "bundle_sha256": "a" * 64,
               "aoip_bundle_sha256": "d" * 64}
        assert _classify_drift(rec, _MANIFEST_BOTH) == "drifted"

    def test_employee_reports_aoip_but_manifest_stale_is_drifted(self):
        """Manifest cũ (chưa publish aoip hash) + agent chạy aoip → bộ đang chạy
        ≠ bộ đã publish → drifted (nhắc operator re-publish), KHÔNG âm thầm current."""
        from gateway.routes.agent_commands import _classify_drift

        rec = {"version": "1.2.0", "bundle_sha256": "a" * 64,
               "aoip_bundle_sha256": "c" * 64}
        assert _classify_drift(rec, _MANIFEST_LEGACY) == "drifted"

    def test_remote_agent_hash_mismatch_still_drifted_regardless_of_aoip(self):
        from gateway.routes.agent_commands import _classify_drift

        rec = {"version": "1.2.0", "bundle_sha256": "b" * 64,
               "aoip_bundle_sha256": "c" * 64}
        assert _classify_drift(rec, _MANIFEST_BOTH) == "drifted"


class TestVersionsEndpointMixedFleet:
    @pytest.mark.asyncio
    async def test_transition_fleet_two_legacy_one_employee(self):
        """Topology transition thật của IT-4: cust-edge/cust-db legacy (không báo
        aoip hash) + cust-app employee. Chỉ employee lệch aoip hash bị drifted."""
        from gateway.routes.agent_commands import (
            _REGISTRY_PREFIX,
            _RELEASE_MANIFEST_KEY,
            list_agent_versions,
        )

        redis = FakeRedis(decode_responses=True)
        await redis.set(_RELEASE_MANIFEST_KEY, json.dumps(_MANIFEST_BOTH))
        now = int(time.time())
        records = {
            "cust-edge": {"version": "1.2.0", "bundle_sha256": "a" * 64},
            "cust-db": {"version": "1.2.0", "bundle_sha256": "a" * 64},
            "cust-app": {"version": "1.2.0", "bundle_sha256": "a" * 64,
                         "aoip_bundle_sha256": "d" * 64},
        }
        for agent_id, extra in records.items():
            rec = {"agent_id": agent_id, "hostname": agent_id, "last_seen": now, **extra}
            await redis.set(f"{_REGISTRY_PREFIX}{agent_id}", json.dumps(rec))

        resp = await list_agent_versions(_make_request(redis))
        data = json.loads(resp.body)

        by_id = {a["agent_id"]: a for a in data["agents"]}
        assert by_id["cust-edge"]["drift_status"] == "current"
        assert by_id["cust-db"]["drift_status"] == "current"
        assert by_id["cust-app"]["drift_status"] == "drifted"
        assert by_id["cust-app"]["aoip_bundle_sha256"] == "d" * 64
        assert data["drifted"] == 1


# ── employee orchestration: 2 vòng song song, shutdown/crash semantics ──────────

class TestEmployeeOrchestration:
    @pytest.mark.asyncio
    async def test_daemon_exit_cancels_telemetry_and_returns_clean(self):
        """SIGTERM → run_daemon trả về → telemetry bị cancel → exit sạch."""
        from aoip.agent.employee import run_employee

        cancelled = asyncio.Event()

        async def fake_telemetry():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def fake_daemon():
            return 1  # daemon dừng (SIGTERM/max_ticks)

        await run_employee(telemetry=fake_telemetry(), daemon=fake_daemon())
        assert cancelled.is_set()

    @pytest.mark.asyncio
    async def test_telemetry_crash_propagates_for_systemd_restart(self):
        """Telemetry chết → exception propagate (exit non-zero) → systemd restart.
        KHÔNG được nuốt lỗi để process 'sống' mà mù telemetry."""
        from aoip.agent.employee import run_employee

        daemon_cancelled = asyncio.Event()

        async def fake_telemetry():
            raise RuntimeError("collector exploded")

        async def fake_daemon():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                daemon_cancelled.set()
                raise

        with pytest.raises(RuntimeError, match="collector exploded"):
            await run_employee(telemetry=fake_telemetry(), daemon=fake_daemon())
        assert daemon_cancelled.is_set()

    @pytest.mark.asyncio
    async def test_daemon_crash_propagates(self):
        from aoip.agent.employee import run_employee

        async def fake_telemetry():
            await asyncio.sleep(3600)

        async def fake_daemon():
            raise RuntimeError("inbox corrupted")

        with pytest.raises(RuntimeError, match="inbox corrupted"):
            await run_employee(telemetry=fake_telemetry(), daemon=fake_daemon())
