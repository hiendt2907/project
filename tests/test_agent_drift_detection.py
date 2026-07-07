"""Sprint NV-SRE IT-2 — agent bundle-hash drift detection.

Covers: canonical bundle hash (agent self-hash ≡ publisher hash), drift
classification contract (current | drifted | unknown), /versions endpoint
surfacing, and the release-manifest publisher script.
"""
from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import MagicMock

import pytest


def _make_request(redis: Any) -> MagicMock:
    req = MagicMock()
    req.app.state.redis = redis
    return req


# ── bundle_hash ───────────────────────────────────────────────────────────────

class TestBundleHash:
    def test_deterministic_and_64_hex(self, tmp_path):
        from remote_agent.bundle_hash import compute_bundle_hash

        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "VERSION").write_text("1.2.0\n")
        h1 = compute_bundle_hash(tmp_path)
        h2 = compute_bundle_hash(tmp_path)
        assert h1 == h2
        assert len(h1) == 64
        int(h1, 16)  # valid hex

    def test_content_change_changes_hash(self, tmp_path):
        from remote_agent.bundle_hash import compute_bundle_hash

        (tmp_path / "a.py").write_text("x = 1\n")
        before = compute_bundle_hash(tmp_path)
        (tmp_path / "a.py").write_text("x = 2\n")
        assert compute_bundle_hash(tmp_path) != before

    def test_version_file_change_changes_hash(self, tmp_path):
        from remote_agent.bundle_hash import compute_bundle_hash

        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "VERSION").write_text("1.2.0\n")
        before = compute_bundle_hash(tmp_path)
        (tmp_path / "VERSION").write_text("1.3.0\n")
        assert compute_bundle_hash(tmp_path) != before

    def test_pycache_and_non_source_ignored(self, tmp_path):
        from remote_agent.bundle_hash import compute_bundle_hash

        (tmp_path / "a.py").write_text("x = 1\n")
        before = compute_bundle_hash(tmp_path)
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "a.cpython-312.pyc").write_bytes(b"\x00bytecode")
        (tmp_path / "notes.txt").write_text("scratch")
        assert compute_bundle_hash(tmp_path) == before

    def test_self_bundle_hash_hashes_running_package(self):
        from remote_agent.bundle_hash import self_bundle_hash

        h = self_bundle_hash()
        assert len(h) == 64


class TestPublisherScript:
    def test_build_manifest_matches_repo_package(self):
        from pathlib import Path

        from remote_agent.bundle_hash import compute_bundle_hash
        from scripts.publish_agent_release import REPO_ROOT, build_manifest

        manifest = build_manifest()
        pkg = REPO_ROOT / "src" / "remote_agent"
        assert manifest["version"] == (pkg / "VERSION").read_text().strip()
        assert manifest["bundle_sha256"] == compute_bundle_hash(Path(pkg))
        assert isinstance(manifest["published_at"], int)


# ── drift classification contract ─────────────────────────────────────────────

_MANIFEST = {"version": "1.2.0", "bundle_sha256": "a" * 64, "published_at": 1}


class TestClassifyDrift:
    def test_no_manifest_is_unknown_never_current(self):
        from gateway.routes.agent_commands import _classify_drift

        rec = {"version": "1.2.0", "bundle_sha256": "a" * 64}
        assert _classify_drift(rec, None) == "unknown"
        assert _classify_drift(rec, {}) == "unknown"

    def test_agent_without_reported_hash_is_unknown(self):
        from gateway.routes.agent_commands import _classify_drift

        assert _classify_drift({"version": "1.2.0"}, _MANIFEST) == "unknown"
        assert _classify_drift({"version": "1.2.0", "bundle_sha256": ""}, _MANIFEST) == "unknown"

    def test_matching_hash_and_version_is_current(self):
        from gateway.routes.agent_commands import _classify_drift

        rec = {"version": "1.2.0", "bundle_sha256": "a" * 64}
        assert _classify_drift(rec, _MANIFEST) == "current"

    def test_hash_mismatch_is_drifted(self):
        from gateway.routes.agent_commands import _classify_drift

        rec = {"version": "1.2.0", "bundle_sha256": "b" * 64}
        assert _classify_drift(rec, _MANIFEST) == "drifted"

    def test_version_mismatch_with_same_hash_is_drifted(self):
        from gateway.routes.agent_commands import _classify_drift

        rec = {"version": "1.1.3", "bundle_sha256": "a" * 64}
        assert _classify_drift(rec, _MANIFEST) == "drifted"


# ── /versions endpoint surfacing ───────────────────────────────────────────────

class TestVersionsEndpointDrift:
    @pytest.mark.asyncio
    async def test_drifted_agent_flagged_within_one_heartbeat_record(self):
        from fakeredis.aioredis import FakeRedis

        from gateway.routes.agent_commands import (
            _REGISTRY_PREFIX,
            _RELEASE_MANIFEST_KEY,
            list_agent_versions,
        )

        redis = FakeRedis(decode_responses=True)
        await redis.set(_RELEASE_MANIFEST_KEY, json.dumps(_MANIFEST))
        now = int(time.time())
        records = {
            "agent-current": {"version": "1.2.0", "bundle_sha256": "a" * 64},
            "agent-stale": {"version": "1.1.3", "bundle_sha256": "b" * 64},
            "agent-legacy": {"version": "1.2.0"},  # predates hash reporting
        }
        for agent_id, extra in records.items():
            rec = {"agent_id": agent_id, "hostname": agent_id, "last_seen": now, **extra}
            await redis.set(f"{_REGISTRY_PREFIX}{agent_id}", json.dumps(rec))

        resp = await list_agent_versions(_make_request(redis))
        data = json.loads(resp.body)

        by_id = {a["agent_id"]: a for a in data["agents"]}
        assert by_id["agent-current"]["drift_status"] == "current"
        assert by_id["agent-stale"]["drift_status"] == "drifted"
        assert by_id["agent-legacy"]["drift_status"] == "unknown"
        assert data["drifted"] == 1
        assert data["release_manifest"]["version"] == "1.2.0"

    @pytest.mark.asyncio
    async def test_no_manifest_everything_unknown(self):
        from fakeredis.aioredis import FakeRedis

        from gateway.routes.agent_commands import _REGISTRY_PREFIX, list_agent_versions

        redis = FakeRedis(decode_responses=True)
        rec = {"agent_id": "a1", "hostname": "h", "version": "1.2.0",
               "bundle_sha256": "a" * 64, "last_seen": int(time.time())}
        await redis.set(f"{_REGISTRY_PREFIX}a1", json.dumps(rec))

        resp = await list_agent_versions(_make_request(redis))
        data = json.loads(resp.body)
        assert data["agents"][0]["drift_status"] == "unknown"
        assert data["drifted"] == 0
        assert data["release_manifest"] is None
