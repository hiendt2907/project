"""Tests for remote_agent.updater — self-update is the ONE write operation
allowed on the agent (INV_NO_WRITE exception). Every guard rail must be
covered: HTTPS-only, host whitelist, mandatory checksum, backup-before-replace,
and rollback on install/restart failure.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from remote_agent import updater


GOOD_CHECKSUM_INPUT = b"agent-binary-content"
GOOD_CHECKSUM = hashlib.sha256(GOOD_CHECKSUM_INPUT).hexdigest()


class TestGetAllowedHosts:
    def test_empty_env_returns_empty_frozenset(self, monkeypatch):
        monkeypatch.delenv(updater._ALLOWED_HOSTS_ENV, raising=False)
        assert updater._get_allowed_hosts() == frozenset()

    def test_comma_separated_hosts_parsed_lowercase(self, monkeypatch):
        monkeypatch.setenv(updater._ALLOWED_HOSTS_ENV, "Cdn.Example.com, releases.example.com")
        assert updater._get_allowed_hosts() == frozenset({"cdn.example.com", "releases.example.com"})


class TestValidateUrl:
    def test_no_allowed_hosts_configured_rejects(self):
        ok, reason = updater._validate_url("https://cdn.example.com/x.tar.gz", frozenset())
        assert ok is False
        assert "not configured" in reason

    def test_non_https_scheme_rejected(self):
        ok, reason = updater._validate_url("http://cdn.example.com/x.tar.gz", frozenset({"cdn.example.com"}))
        assert ok is False
        assert "scheme_not_allowed" in reason

    def test_host_not_in_whitelist_rejected(self):
        ok, reason = updater._validate_url("https://evil.com/x.tar.gz", frozenset({"cdn.example.com"}))
        assert ok is False
        assert "host_not_whitelisted" in reason

    def test_exact_host_match_allowed(self):
        ok, _ = updater._validate_url("https://cdn.example.com/x.tar.gz", frozenset({"cdn.example.com"}))
        assert ok is True

    def test_subdomain_of_whitelisted_host_allowed(self):
        ok, _ = updater._validate_url("https://releases.cdn.example.com/x.tar.gz", frozenset({"cdn.example.com"}))
        assert ok is True

    def test_unrelated_domain_sharing_suffix_rejected(self):
        # "notcdn.example.com" must NOT match "cdn.example.com" by naive substring
        ok, reason = updater._validate_url("https://notcdn.example.com/x", frozenset({"cdn.example.com"}))
        assert ok is False
        assert "host_not_whitelisted" in reason

    def test_malformed_url_rejected_gracefully(self):
        ok, reason = updater._validate_url("not a url at all :::", frozenset({"cdn.example.com"}))
        assert ok is False


class TestSha256File:
    def test_matches_known_hash(self, tmp_path):
        f = tmp_path / "blob.bin"
        f.write_bytes(GOOD_CHECKSUM_INPUT)
        assert updater._sha256_file(f) == GOOD_CHECKSUM


class TestGetInstallDir:
    def test_env_override_used_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv(updater._INSTALL_DIR_ENV, str(tmp_path))
        assert updater._get_install_dir() == tmp_path.resolve()

    def test_falls_back_to_argv0_parent(self, monkeypatch):
        monkeypatch.delenv(updater._INSTALL_DIR_ENV, raising=False)
        result = updater._get_install_dir()
        assert result.is_absolute()


class TestExtractIfArchive:
    def test_single_file_moved_into_install_dir(self, tmp_path):
        src = tmp_path / "src" / "agent-bin"
        src.parent.mkdir()
        src.write_bytes(b"binary")
        install_dir = tmp_path / "install"
        install_dir.mkdir()
        ok, err = updater._extract_if_archive(src, install_dir)
        assert ok is True
        assert (install_dir / "agent-bin").exists()

    def test_tar_gz_extracted_into_install_dir(self, tmp_path):
        import tarfile

        payload = tmp_path / "payload.txt"
        payload.write_text("hello")
        archive = tmp_path / "agent-1.2.0.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(payload, arcname="payload.txt")
        install_dir = tmp_path / "install"
        install_dir.mkdir()
        ok, err = updater._extract_if_archive(archive, install_dir)
        assert ok is True
        assert (install_dir / "payload.txt").exists()

    def test_move_error_returns_false(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        install_dir = tmp_path / "install"
        install_dir.mkdir()
        ok, err = updater._extract_if_archive(missing, install_dir)
        assert ok is False
        assert "move_error" in err


class TestRestartService:
    @pytest.mark.asyncio
    async def test_success(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        with patch("remote_agent.updater.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            ok, err = await updater._restart_service()
        assert ok is True

    @pytest.mark.asyncio
    async def test_nonzero_returncode_fails(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"unit not found"))
        proc.returncode = 1
        with patch("remote_agent.updater.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            ok, err = await updater._restart_service()
        assert ok is False
        assert "unit not found" in err

    @pytest.mark.asyncio
    async def test_timeout_fails(self):
        import asyncio as _asyncio

        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=_asyncio.TimeoutError())
        with patch("remote_agent.updater.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            ok, err = await updater._restart_service()
        assert ok is False
        assert "timed out" in err


class TestDownloadSendsAuthHeader:
    """/webhook/agent/release/bundle sits behind the same _require_api_key
    guard as every other agent route — without an Authorization header the
    download 401s on any cluster with a key configured (i.e. every non-lab
    deployment). Caught live: an agent update to a real OrbStack lab cluster
    failed with http_401 until this header was added."""

    @pytest.mark.asyncio
    async def test_sends_bearer_header_when_api_key_provided(self, tmp_path):
        captured = {}

        class _FakeResp:
            status_code = 200
            content = b"data"

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, url, headers=None):
                captured["headers"] = headers
                return _FakeResp()

        with patch("httpx.AsyncClient", return_value=_FakeClient()):
            ok, err = await updater._download(
                "https://gateway.example.com/bundle", tmp_path / "out", api_key="secret-key",
            )
        assert ok is True
        assert captured["headers"] == {"Authorization": "Bearer secret-key"}

    @pytest.mark.asyncio
    async def test_no_header_when_api_key_empty(self, tmp_path):
        captured = {}

        class _FakeResp:
            status_code = 200
            content = b"data"

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, url, headers=None):
                captured["headers"] = headers
                return _FakeResp()

        with patch("httpx.AsyncClient", return_value=_FakeClient()):
            await updater._download("https://gateway.example.com/bundle", tmp_path / "out")
        assert captured["headers"] is None

    @pytest.mark.asyncio
    async def test_handle_update_command_threads_api_key_to_download(self, monkeypatch):
        monkeypatch.setenv(updater._ALLOWED_HOSTS_ENV, "cdn.example.com")
        with patch("remote_agent.updater._download", AsyncMock(return_value=(False, "x"))) as mock_dl:
            await updater.handle_update_command(
                "c1", "1.2.0", "https://cdn.example.com/x.tar.gz", GOOD_CHECKSUM,
                api_key="agent-secret",
            )
        mock_dl.assert_called_once()
        assert mock_dl.call_args.kwargs["api_key"] == "agent-secret"


class TestHandleUpdateCommand:
    @pytest.mark.asyncio
    async def test_url_blocked_when_no_allowed_hosts(self, monkeypatch):
        monkeypatch.delenv(updater._ALLOWED_HOSTS_ENV, raising=False)
        result = await updater.handle_update_command(
            "c1", "1.2.0", "https://cdn.example.com/x.tar.gz", GOOD_CHECKSUM,
        )
        assert result["update_status"] == "url_blocked"
        assert result["rc"] == 1

    @pytest.mark.asyncio
    async def test_checksum_missing_rejected_before_download(self, monkeypatch):
        monkeypatch.setenv(updater._ALLOWED_HOSTS_ENV, "cdn.example.com")
        with patch("remote_agent.updater._download", AsyncMock()) as mock_dl:
            result = await updater.handle_update_command(
                "c1", "1.2.0", "https://cdn.example.com/x.tar.gz", "short",
            )
        mock_dl.assert_not_called()
        assert result["update_status"] == "checksum_missing"

    @pytest.mark.asyncio
    async def test_download_failure_returns_download_fail(self, monkeypatch, tmp_path):
        monkeypatch.setenv(updater._ALLOWED_HOSTS_ENV, "cdn.example.com")
        with patch("remote_agent.updater._download", AsyncMock(return_value=(False, "connection_refused"))):
            result = await updater.handle_update_command(
                "c1", "1.2.0", "https://cdn.example.com/x.tar.gz", GOOD_CHECKSUM,
            )
        assert result["update_status"] == "download_fail"
        assert "connection_refused" in result["stderr"]

    @pytest.mark.asyncio
    async def test_checksum_mismatch_aborts_install(self, monkeypatch, tmp_path):
        monkeypatch.setenv(updater._ALLOWED_HOSTS_ENV, "cdn.example.com")

        async def fake_download(url, dest, api_key=""):
            dest.write_bytes(b"different-content-than-expected")
            return True, ""

        with patch("remote_agent.updater._download", fake_download):
            result = await updater.handle_update_command(
                "c1", "1.2.0", "https://cdn.example.com/x.tar.gz", GOOD_CHECKSUM,
            )
        assert result["update_status"] == "checksum_fail"

    @pytest.mark.asyncio
    async def test_full_success_path_downloads_backs_up_installs_restarts(self, monkeypatch, tmp_path):
        monkeypatch.setenv(updater._ALLOWED_HOSTS_ENV, "cdn.example.com")
        install_dir = tmp_path / "install"
        install_dir.mkdir()
        (install_dir / "old-binary").write_bytes(b"old")
        monkeypatch.setenv(updater._INSTALL_DIR_ENV, str(install_dir))

        async def fake_download(url, dest, api_key=""):
            dest.write_bytes(GOOD_CHECKSUM_INPUT)
            return True, ""

        with patch("remote_agent.updater._download", fake_download), \
             patch("remote_agent.updater._restart_service", AsyncMock(return_value=(True, ""))):
            result = await updater.handle_update_command(
                "c1", "1.2.0", "https://cdn.example.com/agent-bin", GOOD_CHECKSUM,
                current_version="1.1.0",
            )
        assert result["update_status"] == "success"
        installed = [p for p in install_dir.iterdir() if p.name != "old-binary"]
        assert len(installed) == 1

    @pytest.mark.asyncio
    async def test_restart_failure_triggers_rollback(self, monkeypatch, tmp_path):
        monkeypatch.setenv(updater._ALLOWED_HOSTS_ENV, "cdn.example.com")
        install_dir = tmp_path / "install"
        install_dir.mkdir()
        (install_dir / "old-binary").write_bytes(b"old-content")
        monkeypatch.setenv(updater._INSTALL_DIR_ENV, str(install_dir))

        async def fake_download(url, dest, api_key=""):
            dest.write_bytes(GOOD_CHECKSUM_INPUT)
            return True, ""

        with patch("remote_agent.updater._download", fake_download), \
             patch("remote_agent.updater._restart_service", AsyncMock(return_value=(False, "service failed to start"))):
            result = await updater.handle_update_command(
                "c1", "1.2.0", "https://cdn.example.com/agent-bin", GOOD_CHECKSUM,
                current_version="1.1.0",
            )
        assert result["update_status"] == "restart_fail"
