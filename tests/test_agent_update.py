"""Unit tests for remote agent self-update mechanism."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Updater module tests ──────────────────────────────────────────────────────

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_update_url_blocked_when_no_allowed_hosts():
    from remote_agent.updater import handle_update_command
    with patch.dict("os.environ", {"OMNI_AGENT_UPDATE_ALLOWED_HOSTS": ""}):
        result = await handle_update_command(
            cmd_id="cmd-001",
            version="1.1.0",
            download_url="https://example.com/agent.tar.gz",
            sha256_checksum="abc" * 22,
        )
    assert result["rc"] == 1
    assert result["update_status"] == "url_blocked"


@pytest.mark.asyncio
async def test_update_url_blocked_when_host_not_whitelisted():
    from remote_agent.updater import handle_update_command
    with patch.dict("os.environ", {"OMNI_AGENT_UPDATE_ALLOWED_HOSTS": "trusted.example.com"}):
        result = await handle_update_command(
            cmd_id="cmd-002",
            version="1.1.0",
            download_url="https://evil.attacker.com/agent.tar.gz",
            sha256_checksum="abc" * 22,
        )
    assert result["rc"] == 1
    assert result["update_status"] == "url_blocked"
    assert "host_not_whitelisted" in result["stderr"]


@pytest.mark.asyncio
async def test_update_url_blocked_non_https():
    from remote_agent.updater import handle_update_command
    with patch.dict("os.environ", {"OMNI_AGENT_UPDATE_ALLOWED_HOSTS": "example.com"}):
        result = await handle_update_command(
            cmd_id="cmd-003",
            version="1.1.0",
            download_url="http://example.com/agent.tar.gz",  # http not https
            sha256_checksum="abc" * 22,
        )
    assert result["update_status"] == "url_blocked"
    assert "scheme_not_allowed" in result["stderr"]


@pytest.mark.asyncio
async def test_checksum_missing_aborts():
    from remote_agent.updater import handle_update_command
    with patch.dict("os.environ", {"OMNI_AGENT_UPDATE_ALLOWED_HOSTS": "example.com"}):
        result = await handle_update_command(
            cmd_id="cmd-004",
            version="1.1.0",
            download_url="https://example.com/agent.tar.gz",
            sha256_checksum="",  # empty
        )
    assert result["update_status"] == "checksum_missing"
    assert result["rc"] == 1


@pytest.mark.asyncio
async def test_checksum_mismatch_aborts_and_cleans_tmp():
    """Download succeeds but sha256 mismatch → abort, no binary change."""
    from remote_agent.updater import handle_update_command

    fake_content = b"fake agent binary"
    wrong_checksum = "a" * 64  # wrong hash

    async def mock_download(url: str, dest: Path) -> tuple[bool, str]:
        dest.write_bytes(fake_content)
        return True, ""

    with (
        patch.dict("os.environ", {"OMNI_AGENT_UPDATE_ALLOWED_HOSTS": "example.com"}),
        patch("remote_agent.updater._download", mock_download),
    ):
        result = await handle_update_command(
            cmd_id="cmd-005",
            version="1.1.0",
            download_url="https://example.com/agent.tar.gz",
            sha256_checksum=wrong_checksum,
        )

    assert result["update_status"] == "checksum_fail"
    assert result["rc"] == 1
    # tmp file must not remain
    tmp_new = Path(f"/tmp/omni-agent-new-1.1.0-cmd-0050.tar.gz")
    assert not tmp_new.exists()


@pytest.mark.asyncio
async def test_download_fail_returns_error():
    from remote_agent.updater import handle_update_command

    async def mock_download(url: str, dest: Path) -> tuple[bool, str]:
        return False, "http_404"

    with (
        patch.dict("os.environ", {"OMNI_AGENT_UPDATE_ALLOWED_HOSTS": "example.com"}),
        patch("remote_agent.updater._download", mock_download),
    ):
        result = await handle_update_command(
            cmd_id="cmd-006",
            version="1.1.0",
            download_url="https://example.com/agent.tar.gz",
            sha256_checksum="a" * 64,
        )

    assert result["update_status"] == "download_fail"
    assert "http_404" in result["stderr"]


@pytest.mark.asyncio
async def test_successful_update_calls_restart(tmp_path):
    """Full happy path: download + verify + extract + restart → success."""
    from remote_agent.updater import handle_update_command

    fake_content = b"agent binary content"
    correct_checksum = _sha256_bytes(fake_content)

    async def mock_download(url: str, dest: Path) -> tuple[bool, str]:
        dest.write_bytes(fake_content)
        return True, ""

    def mock_extract(src: Path, install_dir: Path) -> tuple[bool, str]:
        return True, ""

    async def mock_restart() -> tuple[bool, str]:
        return True, ""

    with (
        patch.dict("os.environ", {
            "OMNI_AGENT_UPDATE_ALLOWED_HOSTS": "example.com",
            "OMNI_AGENT_INSTALL_DIR": str(tmp_path),
        }),
        patch("remote_agent.updater._download", mock_download),
        patch("remote_agent.updater._extract_if_archive", mock_extract),
        patch("remote_agent.updater._restart_service", mock_restart),
    ):
        result = await handle_update_command(
            cmd_id="cmd-007",
            version="1.1.0",
            download_url="https://example.com/agent.tar.gz",
            sha256_checksum=correct_checksum,
            current_version="1.0.0",
        )

    assert result["update_status"] == "success"
    assert result["rc"] == 0
    assert result["update_version"] == "1.1.0"


@pytest.mark.asyncio
async def test_restart_fail_restores_backup(tmp_path):
    """If restart fails → rollback and return restart_fail."""
    from remote_agent.updater import handle_update_command

    fake_content = b"new agent binary"
    correct_checksum = _sha256_bytes(fake_content)

    async def mock_download(url: str, dest: Path) -> tuple[bool, str]:
        dest.write_bytes(fake_content)
        return True, ""

    def mock_extract(src: Path, install_dir: Path) -> tuple[bool, str]:
        return True, ""

    restart_calls: list[str] = []

    async def mock_restart() -> tuple[bool, str]:
        restart_calls.append("restart")
        return False, "systemctl: unit not found"

    with (
        patch.dict("os.environ", {
            "OMNI_AGENT_UPDATE_ALLOWED_HOSTS": "example.com",
            "OMNI_AGENT_INSTALL_DIR": str(tmp_path),
        }),
        patch("remote_agent.updater._download", mock_download),
        patch("remote_agent.updater._extract_if_archive", mock_extract),
        patch("remote_agent.updater._restart_service", mock_restart),
    ):
        result = await handle_update_command(
            cmd_id="cmd-008",
            version="1.1.0",
            download_url="https://example.com/agent.tar.gz",
            sha256_checksum=correct_checksum,
            current_version="1.0.0",
        )

    assert result["update_status"] == "restart_fail"
    assert result["rc"] == 1


# ── command_executor routing tests ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_batch_routes_update_agent():
    """UPDATE_AGENT type is routed to handle_update_command, not execute_command."""
    from remote_agent.command_executor import execute_batch

    update_cmd = {
        "cmd_id": "upd-abc123",
        "type": "UPDATE_AGENT",
        "version": "2.0.0",
        "download_url": "https://cdn.example.com/agent.tar.gz",
        "sha256_checksum": "a" * 64,
    }
    expected_result = {
        "cmd_id": "upd-abc123",
        "blocked": False,
        "stdout": "UPDATE_AGENT status=success",
        "stderr": "",
        "rc": 0,
        "duration_ms": 5,
        "update_status": "success",
        "update_version": "2.0.0",
    }

    with patch("remote_agent.updater.handle_update_command", new_callable=AsyncMock) as mock_upd:
        mock_upd.return_value = expected_result
        results = await execute_batch([update_cmd], current_version="1.0.0")

    mock_upd.assert_called_once_with(
        cmd_id="upd-abc123",
        version="2.0.0",
        download_url="https://cdn.example.com/agent.tar.gz",
        sha256_checksum="a" * 64,
        current_version="1.0.0",
    )
    assert results[0]["update_status"] == "success"


@pytest.mark.asyncio
async def test_execute_batch_regular_command_not_routed_to_updater():
    """Regular commands go through whitelist, not updater."""
    from remote_agent.command_executor import execute_batch

    regular_cmd = {
        "cmd_id": "cmd-xyz",
        "command": "df",
        "args": ["-h"],
        "timeout_s": 10,
    }

    with (
        patch("remote_agent.updater.handle_update_command", new_callable=AsyncMock) as mock_upd,
        patch("remote_agent.command_executor.execute_command", new_callable=AsyncMock) as mock_exec,
    ):
        mock_exec.return_value = {"cmd_id": "cmd-xyz", "rc": 0, "stdout": "/dev 100G", "stderr": "", "blocked": False, "duration_ms": 10}
        await execute_batch([regular_cmd])

    mock_upd.assert_not_called()
    mock_exec.assert_called_once()


# ── Gateway endpoint tests ────────────────────────────────────────────────────

def _make_request(redis: Any) -> MagicMock:
    req = MagicMock()
    req.app.state.redis = redis
    return req


@pytest.mark.asyncio
async def test_enqueue_agent_update_blocked_no_allowed_hosts():
    from gateway.routes.agent_commands import enqueue_agent_update, UpdateAgentRequest
    from fakeredis.aioredis import FakeRedis

    redis = FakeRedis(decode_responses=True)
    body = UpdateAgentRequest(
        agent_id="agent-1",
        version="1.1.0",
        download_url="https://cdn.example.com/agent.tar.gz",
        sha256_checksum="a" * 64,
    )
    req = _make_request(redis)

    import fastapi
    with (
        patch.dict("os.environ", {"OMNI_AGENT_UPDATE_ALLOWED_HOSTS": ""}),
        pytest.raises(fastapi.HTTPException) as exc_info,
    ):
        await enqueue_agent_update(body, req)

    assert exc_info.value.status_code == 422
    assert "update_url_blocked" in exc_info.value.detail


@pytest.mark.asyncio
async def test_enqueue_agent_update_success():
    from gateway.routes.agent_commands import enqueue_agent_update, UpdateAgentRequest
    from fakeredis.aioredis import FakeRedis

    redis = FakeRedis(decode_responses=True)
    body = UpdateAgentRequest(
        agent_id="agent-1",
        version="1.1.0",
        download_url="https://cdn.example.com/agent.tar.gz",
        sha256_checksum="b" * 64,
    )
    req = _make_request(redis)

    with patch.dict("os.environ", {"OMNI_AGENT_UPDATE_ALLOWED_HOSTS": "cdn.example.com"}):
        resp = await enqueue_agent_update(body, req)

    data = json.loads(resp.body)
    assert data["status"] == "enqueued"
    assert data["agent_id"] == "agent-1"
    assert data["version"] == "1.1.0"
    assert data["cmd_id"].startswith("upd-")

    # Verify command was pushed into Redis queue
    queue_key = "omni:agent:cmd:agent-1"
    raw = await redis.rpop(queue_key)
    assert raw is not None
    cmd = json.loads(raw)
    assert cmd["type"] == "UPDATE_AGENT"
    assert cmd["version"] == "1.1.0"
    assert cmd["sha256_checksum"] == "b" * 64


@pytest.mark.asyncio
async def test_list_agent_versions_empty():
    from gateway.routes.agent_commands import list_agent_versions
    from fakeredis.aioredis import FakeRedis

    redis = FakeRedis(decode_responses=True)
    req = _make_request(redis)
    resp = await list_agent_versions(req)
    data = json.loads(resp.body)
    assert data["agents"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_agent_versions_returns_registered_agents():
    from gateway.routes.agent_commands import list_agent_versions, _REGISTRY_PREFIX
    from fakeredis.aioredis import FakeRedis

    redis = FakeRedis(decode_responses=True)
    now = int(time.time())

    for i in range(2):
        rec = {
            "agent_id": f"agent-{i}",
            "hostname": f"vm-{i}.example.com",
            "version": f"1.{i}.0",
            "capabilities": ["metrics"],
            "last_seen": now - i * 10,
        }
        await redis.set(f"{_REGISTRY_PREFIX}agent-{i}", json.dumps(rec))

    req = _make_request(redis)
    resp = await list_agent_versions(req)
    data = json.loads(resp.body)

    assert data["total"] == 2
    versions = {a["agent_id"]: a["version"] for a in data["agents"]}
    assert versions["agent-0"] == "1.0.0"
    assert versions["agent-1"] == "1.1.0"
    # agent-0 just seen → online
    agent0 = next(a for a in data["agents"] if a["agent_id"] == "agent-0")
    assert agent0["online"] is True


@pytest.mark.asyncio
async def test_list_agent_versions_marks_stale_offline():
    from gateway.routes.agent_commands import list_agent_versions, _REGISTRY_PREFIX
    from fakeredis.aioredis import FakeRedis

    redis = FakeRedis(decode_responses=True)
    now = int(time.time())
    rec = {
        "agent_id": "agent-stale",
        "hostname": "old.example.com",
        "version": "0.9.0",
        "capabilities": [],
        "last_seen": now - 200,  # 200s ago = stale
    }
    await redis.set(f"{_REGISTRY_PREFIX}agent-stale", json.dumps(rec))

    req = _make_request(redis)
    resp = await list_agent_versions(req)
    data = json.loads(resp.body)
    assert data["agents"][0]["online"] is False


# ── URL validation helper tests ───────────────────────────────────────────────

def test_validate_update_url_subdomain_allowed():
    with patch.dict("os.environ", {"OMNI_AGENT_UPDATE_ALLOWED_HOSTS": "example.com"}):
        from gateway.routes import agent_commands
        # reload to pick up env
        ok, _ = agent_commands._validate_update_url("https://cdn.example.com/v1/agent.tar.gz")
        assert ok


def test_validate_update_url_exact_host_allowed():
    with patch.dict("os.environ", {"OMNI_AGENT_UPDATE_ALLOWED_HOSTS": "releases.mycompany.io"}):
        from gateway.routes import agent_commands
        ok, _ = agent_commands._validate_update_url("https://releases.mycompany.io/agent-1.2.tar.gz")
        assert ok


def test_validate_update_url_foreign_host_blocked():
    with patch.dict("os.environ", {"OMNI_AGENT_UPDATE_ALLOWED_HOSTS": "example.com"}):
        from gateway.routes import agent_commands
        ok, reason = agent_commands._validate_update_url("https://attacker.evil.io/agent.tar.gz")
        assert not ok
        assert "host_not_whitelisted" in reason
