"""Sprint NV-SRE IT-5 — safe self-update qua durable command channel.

Phủ: verb routing, checksum fail-closed, backup N-1 + extract, pending marker,
startup_gate (commit / rollback), reconciler (result marker + fallback disk-state),
gateway /webhook/agent/release/bundle, guard shell crash-loop (chạy bash thật),
publisher tarball deterministic + manifest release_tar_sha256.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from aoip.agent import updater
from aoip.agent.updater import (
    STATUS_ROLLED_BACK,
    STATUS_UPDATED,
    apply_update,
    make_update_executor,
    make_update_reconciler,
    restore_previous,
    startup_gate,
)
from remote_agent.bundle_hash import compute_bundle_hash

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_install(tmp_path: Path, marker: str = "old") -> Path:
    """Install dir tối thiểu: 2 package với 1 file .py mỗi cái + VERSION."""
    install = tmp_path / "install"
    for pkg in ("remote_agent", "aoip"):
        d = install / pkg
        d.mkdir(parents=True)
        (d / "__init__.py").write_text(f"# {pkg} {marker}\n")
    (install / "remote_agent" / "VERSION").write_text("9.9.9\n")
    return install


def _make_bundle(marker: str = "new") -> bytes:
    """Tarball hợp lệ chứa 2 package (layout khớp _BUNDLE_PACKAGES)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for pkg in ("remote_agent", "aoip"):
            data = f"# {pkg} {marker}\n".encode()
            info = tarfile.TarInfo(name=f"{pkg}/__init__.py")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        vdata = b"9.9.9\n"
        vinfo = tarfile.TarInfo(name="remote_agent/VERSION")
        vinfo.size = len(vdata)
        tar.addfile(vinfo, io.BytesIO(vdata))
    return gzip.compress(buf.getvalue(), mtime=0)


def _payload_for(bundle: bytes, install: Path | None = None, *, version="9.9.9") -> dict:
    p = {"verb": "UPDATE_AGENT", "version": version, "command_id": "cmd-upd-1",
         "release_tar_sha256": hashlib.sha256(bundle).hexdigest()}
    if install is not None:
        # expected hash = hash SAU khi extract bundle này (tính trên staging riêng)
        stage = install.parent / "stage-expected"
        for pkg in ("remote_agent", "aoip"):
            (stage / pkg).mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
            tar.extractall(stage)  # noqa: S202 — fixture cục bộ
        p["bundle_sha256"] = compute_bundle_hash(stage / "remote_agent")
        p["aoip_bundle_sha256"] = compute_bundle_hash(stage / "aoip")
    return p


# ── apply_update ─────────────────────────────────────────────────────────────

class TestApplyUpdate:
    def test_checksum_mismatch_fail_closed_install_untouched(self, tmp_path):
        install = _make_install(tmp_path)
        before = compute_bundle_hash(install / "remote_agent")
        bundle = _make_bundle()
        result = apply_update({"verb": "UPDATE_AGENT", "version": "9.9.9",
                               "release_tar_sha256": "0" * 64},
                              bundle, install_dir=install, releases_dir=tmp_path / "rel")
        assert result["update_status"] == "checksum_fail"
        assert compute_bundle_hash(install / "remote_agent") == before
        assert not (tmp_path / "rel" / "pending.json").exists()

    def test_invalid_payload_rejected(self, tmp_path):
        install = _make_install(tmp_path)
        result = apply_update({"verb": "UPDATE_AGENT"}, b"x",
                              install_dir=install, releases_dir=tmp_path / "rel")
        assert result["update_status"] == "invalid_payload"

    def test_success_extracts_backs_up_and_writes_pending(self, tmp_path):
        install = _make_install(tmp_path, marker="old")
        old_hash = compute_bundle_hash(install / "aoip")
        bundle = _make_bundle(marker="new")
        releases = tmp_path / "rel"
        result = apply_update(_payload_for(bundle, install), bundle,
                              install_dir=install, releases_dir=releases)
        assert result.get("ok") is True
        # code mới đã nằm trên đĩa
        assert "new" in (install / "aoip" / "__init__.py").read_text()
        assert compute_bundle_hash(install / "aoip") != old_hash
        # backup N-1 + pending marker
        assert (releases / "previous.tar.gz").exists()
        pending = json.loads((releases / "pending.json").read_text())
        assert pending["version"] == "9.9.9"
        assert pending["command_id"] == "cmd-upd-1"
        assert pending["expected"]["aoip_bundle_sha256"]

    def test_restore_previous_brings_back_old_code(self, tmp_path):
        install = _make_install(tmp_path, marker="old")
        bundle = _make_bundle(marker="new")
        releases = tmp_path / "rel"
        apply_update(_payload_for(bundle, install), bundle,
                     install_dir=install, releases_dir=releases)
        assert restore_previous(install, releases) is True
        assert "old" in (install / "aoip" / "__init__.py").read_text()

    def test_restore_without_backup_returns_false(self, tmp_path):
        install = _make_install(tmp_path)
        assert restore_previous(install, tmp_path / "empty") is False


# ── startup_gate (health-gate boot mới) ──────────────────────────────────────

class TestStartupGate:
    def test_no_pending_returns_none(self, tmp_path):
        install = _make_install(tmp_path)
        assert startup_gate(install_dir=install, releases_dir=tmp_path / "rel") is None

    def test_hash_match_commits_updated(self, tmp_path):
        install = _make_install(tmp_path)
        bundle = _make_bundle(marker="new")
        releases = tmp_path / "rel"
        apply_update(_payload_for(bundle, install), bundle,
                     install_dir=install, releases_dir=releases)
        gate = startup_gate(install_dir=install, releases_dir=releases)
        assert gate["update_status"] == STATUS_UPDATED
        assert gate["needs_restart"] is False
        assert not (releases / "pending.json").exists()
        result = json.loads((releases / "result.json").read_text())
        assert result["update_status"] == STATUS_UPDATED
        assert result["command_id"] == "cmd-upd-1"

    def test_hash_mismatch_rolls_back_to_previous(self, tmp_path):
        install = _make_install(tmp_path, marker="old")
        bundle = _make_bundle(marker="new")
        releases = tmp_path / "rel"
        apply_update(_payload_for(bundle, install), bundle,
                     install_dir=install, releases_dir=releases)
        # Giả lập bundle hỏng-lệch: sửa file sau extract → self-hash != expected
        (install / "aoip" / "__init__.py").write_text("# tampered\n")
        gate = startup_gate(install_dir=install, releases_dir=releases)
        assert gate["update_status"] == STATUS_ROLLED_BACK
        assert gate["needs_restart"] is True
        # N-1 đã được restore
        assert "old" in (install / "aoip" / "__init__.py").read_text()
        assert json.loads((releases / "result.json").read_text())["update_status"] == STATUS_ROLLED_BACK


# ── executor verb routing ────────────────────────────────────────────────────

class TestUpdateExecutor:
    async def test_non_update_verb_delegates_to_base(self, tmp_path):
        calls = []

        async def base(payload):
            calls.append(payload)
            return "COMPLETED", {"rc": 0}

        ex = make_update_executor(base, client=None, install_dir=tmp_path,
                                  releases_dir=tmp_path / "rel")
        state, outcome = await ex({"verb": "RESTART_SERVICE"})
        assert state == "COMPLETED" and calls

    async def test_download_failure_is_outcome_not_crash(self, tmp_path):
        class _Client:
            async def download_release_bundle(self):
                raise RuntimeError("gateway unreachable")

        ex = make_update_executor(None, client=_Client(), install_dir=tmp_path,
                                  releases_dir=tmp_path / "rel")
        state, outcome = await ex({"verb": "UPDATE_AGENT", "version": "1", "release_tar_sha256": "a" * 64})
        assert state == "FAILED"
        assert outcome["update_status"] == "download_fail"

    async def test_checksum_fail_reports_failed(self, tmp_path):
        bundle = _make_bundle()

        class _Client:
            async def download_release_bundle(self):
                return bundle

        install = _make_install(tmp_path)
        ex = make_update_executor(None, client=_Client(), install_dir=install,
                                  releases_dir=tmp_path / "rel")
        state, outcome = await ex({"verb": "UPDATE_AGENT", "version": "9.9.9",
                                   "release_tar_sha256": "f" * 64})
        assert state == "FAILED"
        assert outcome["update_status"] == "checksum_fail"

    async def test_success_calls_restart_and_blocks(self, tmp_path):
        bundle = _make_bundle(marker="new")
        install = _make_install(tmp_path)
        restarted = asyncio.Event()

        class _Client:
            async def download_release_bundle(self):
                return bundle

        async def fake_restart():
            restarted.set()

        ex = make_update_executor(None, client=_Client(), install_dir=install,
                                  releases_dir=tmp_path / "rel", restart=fake_restart)
        task = asyncio.ensure_future(ex(_payload_for(bundle, install)))
        await asyncio.wait_for(restarted.wait(), timeout=5)
        # executor CHỜ VÔ HẠN sau restart (process sẽ chết thật ngoài đời) — không return
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert not done
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (tmp_path / "rel" / "pending.json").exists()


# ── reconciler (resume sau restart) ──────────────────────────────────────────

class TestUpdateReconciler:
    def _entry(self, payload: dict) -> SimpleNamespace:
        return SimpleNamespace(command_id=payload.get("command_id", "cmd-upd-1"), payload=payload)

    async def test_result_marker_updated_reports_completed_once(self, tmp_path):
        install = _make_install(tmp_path)
        releases = tmp_path / "rel"
        releases.mkdir()
        (releases / "result.json").write_text(json.dumps(
            {"update_status": STATUS_UPDATED, "version": "9.9.9", "command_id": "cmd-upd-1"}))
        rec = make_update_reconciler(install_dir=install, releases_dir=releases)
        state, outcome = await rec(self._entry({"verb": "UPDATE_AGENT", "version": "9.9.9"}))
        assert state == "COMPLETED" and outcome["update_status"] == STATUS_UPDATED
        assert not (releases / "result.json").exists()  # marker dọn sau khi đọc

    async def test_result_marker_rolled_back_reports_failed(self, tmp_path):
        install = _make_install(tmp_path)
        releases = tmp_path / "rel"
        releases.mkdir()
        (releases / "result.json").write_text(json.dumps(
            {"update_status": STATUS_ROLLED_BACK, "version": "9.9.9", "command_id": "cmd-upd-1"}))
        rec = make_update_reconciler(install_dir=install, releases_dir=releases)
        state, outcome = await rec(self._entry({"verb": "UPDATE_AGENT"}))
        assert state == "FAILED" and outcome["update_status"] == STATUS_ROLLED_BACK

    async def test_missing_marker_derives_from_disk_hash(self, tmp_path):
        install = _make_install(tmp_path)
        rec = make_update_reconciler(install_dir=install, releases_dir=tmp_path / "rel")
        actual_aoip = compute_bundle_hash(install / "aoip")
        state, outcome = await rec(self._entry(
            {"verb": "UPDATE_AGENT", "version": "9.9.9", "aoip_bundle_sha256": actual_aoip}))
        assert state == "COMPLETED"
        state2, outcome2 = await rec(self._entry(
            {"verb": "UPDATE_AGENT", "version": "9.9.9", "aoip_bundle_sha256": "f" * 64}))
        assert state2 == "FAILED" and outcome2["update_status"] == STATUS_ROLLED_BACK

    async def test_non_update_verb_escalates_no_blind_retry(self, tmp_path):
        install = _make_install(tmp_path)
        rec = make_update_reconciler(install_dir=install, releases_dir=tmp_path / "rel")
        state, outcome = await rec(self._entry({"verb": "RESTART_SERVICE"}))
        assert state == "ESCALATED"
        assert outcome["reason"] == "crash_during_execution_unknown_outcome"


# ── gateway /webhook/agent/release/bundle ────────────────────────────────────

class TestGatewayReleaseBundle:
    def _app(self):
        from fakeredis.aioredis import FakeRedis
        from fastapi import FastAPI

        from gateway.routes.agent_commands import router

        app = FastAPI()
        app.state.redis = FakeRedis(decode_responses=True)
        app.include_router(router)
        return app

    async def test_404_when_not_published(self):
        import httpx

        app = self._app()
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/webhook/agent/release/bundle")
        assert resp.status_code == 404

    async def test_streams_decoded_bytes(self):
        import httpx

        app = self._app()
        bundle = _make_bundle()
        await app.state.redis.set("omni:agent:release_bundle",
                                  base64.b64encode(bundle).decode())
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/webhook/agent/release/bundle")
        assert resp.status_code == 200
        assert resp.content == bundle
        assert resp.headers["content-type"] == "application/gzip"


# ── publisher: tarball deterministic + manifest ──────────────────────────────

class TestPublisher:
    def test_release_tar_deterministic_and_manifest_has_tar_hash(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "publish_agent_release", REPO_ROOT / "scripts" / "publish_agent_release.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        tar1 = mod.build_release_tar()
        tar2 = mod.build_release_tar()
        assert hashlib.sha256(tar1).hexdigest() == hashlib.sha256(tar2).hexdigest()

        manifest = mod.build_manifest(tar1)
        assert manifest["release_tar_sha256"] == hashlib.sha256(tar1).hexdigest()
        assert manifest["bundle_sha256"] and manifest["aoip_bundle_sha256"]
        # tarball bung ra phải tái tạo đúng bundle hash publish (agent gate so được)
        stage = Path(REPO_ROOT / "dist" / ".tar-hash-check")
        import shutil

        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(tar1), mode="r:gz") as tar:
            tar.extractall(stage)  # noqa: S202 — nội dung do chính repo build
        assert compute_bundle_hash(stage / "remote_agent") == manifest["bundle_sha256"]
        assert compute_bundle_hash(stage / "aoip") == manifest["aoip_bundle_sha256"]
        shutil.rmtree(stage, ignore_errors=True)


# ── guard shell (crash-loop, NGOÀI bundle) ───────────────────────────────────

class TestGuardShell:
    GUARD = REPO_ROOT / "scripts" / "aoip-agent-guard.sh"

    def _run(self, install: Path, releases: Path):
        return subprocess.run(
            ["bash", str(self.GUARD)], capture_output=True, text=True,
            env={"OMNI_AGENT_INSTALL_DIR": str(install),
                 "AOIP_RELEASES_DIR": str(releases), "PATH": "/usr/bin:/bin"})

    def test_normal_boot_clears_bootcount(self, tmp_path):
        install = _make_install(tmp_path)
        releases = tmp_path / "rel"
        releases.mkdir()
        (releases / "bootcount").write_text("2")
        assert self._run(install, releases).returncode == 0
        assert not (releases / "bootcount").exists()

    def test_rolls_back_after_max_boot_attempts(self, tmp_path):
        install = _make_install(tmp_path, marker="old")
        releases = tmp_path / "rel"
        # apply update thật để có backup + pending, rồi giả lập bundle hỏng
        bundle = _make_bundle(marker="broken")
        apply_update(_payload_for(bundle, install), bundle,
                     install_dir=install, releases_dir=releases)
        assert "broken" in (install / "aoip" / "__init__.py").read_text()

        for boot in range(1, 4):  # 3 boot đầu: chỉ đếm, không rollback
            assert self._run(install, releases).returncode == 0
            assert (releases / "pending.json").exists(), f"boot {boot}"
        # boot 4 (> MAX 3): restore N-1 + result rolled_back
        assert self._run(install, releases).returncode == 0
        assert not (releases / "pending.json").exists()
        assert "old" in (install / "aoip" / "__init__.py").read_text()
        result = json.loads((releases / "result.json").read_text())
        assert result["update_status"] == STATUS_ROLLED_BACK
        assert result["command_id"] == "cmd-upd-1"
        assert "crash_loop_guard" in result["detail"]
