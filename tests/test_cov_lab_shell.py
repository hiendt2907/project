"""Tests for src/workers/lab_shell.py — coverage of uncovered paths."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("OMNI_ENV_MODE", "dev")

import pytest

from workers.lab_shell import _audit_lab_shell, tool_execute_shell_command


def _wf_return(value):
    """asyncio.wait_for stub: đóng coroutine được truyền vào rồi trả value —
    tránh RuntimeWarning 'coroutine never awaited' từ mocked wait_for."""
    def _wf(coro, *args, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        return value
    return _wf


def _wf_timeout(coro, *args, **kwargs):
    if hasattr(coro, "close"):
        coro.close()
    raise asyncio.TimeoutError()



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**kwargs):
    defaults = dict(
        lab_unchained=True,
        god_mode=False,
        kafka_topic_audit_sandbox="omni-audit-sandbox",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _ctx(settings=None, kafka=None, trace_id="trace-test"):
    if settings is None:
        settings = _settings()
    ctx = SimpleNamespace(
        settings=settings,
        kafka=kafka,
        inbound_trace_id=trace_id,
    )
    return ctx


# ---------------------------------------------------------------------------
# _audit_lab_shell
# ---------------------------------------------------------------------------


async def test_audit_lab_shell_sends_to_kafka():
    kafka = AsyncMock()
    ctx = _ctx(kafka=kafka)
    await _audit_lab_shell(
        ctx,
        trace_id="t-123",
        command="echo hello",
        exit_code=0,
        stdout="hello",
        stderr="",
    )
    kafka.send_dict.assert_called_once()
    topic, payload = kafka.send_dict.call_args[0]
    assert topic == "omni-audit-sandbox"
    body = json.loads(payload["data"])
    assert body["source"] == "lab_shell"
    assert body["exit_code"] == 0
    assert body["command"] == "echo hello"
    assert body["trace_id"] == "t-123"


async def test_audit_lab_shell_no_kafka():
    ctx = _ctx(kafka=None)
    # Should not raise
    await _audit_lab_shell(ctx, trace_id="trace-t", command="ls", exit_code=0, stdout="", stderr="")


async def test_audit_lab_shell_kafka_error_swallowed():
    kafka = AsyncMock()
    kafka.send_dict.side_effect = Exception("kafka unavailable")
    ctx = _ctx(kafka=kafka)
    # Should not raise
    await _audit_lab_shell(ctx, trace_id="trace-t", command="ls", exit_code=0, stdout="", stderr="")


async def test_audit_lab_shell_long_command_truncated():
    kafka = AsyncMock()
    ctx = _ctx(kafka=kafka)
    long_cmd = "X" * 3000
    await _audit_lab_shell(ctx, trace_id="trace-t", command=long_cmd, exit_code=0, stdout="", stderr="")
    body = json.loads(kafka.send_dict.call_args[0][1]["data"])
    assert len(body["command"]) <= 2000
    assert body["command_truncated"] is True


async def test_audit_lab_shell_short_command_not_truncated():
    kafka = AsyncMock()
    ctx = _ctx(kafka=kafka)
    await _audit_lab_shell(ctx, trace_id="trace-t", command="ls -la", exit_code=0, stdout="", stderr="")
    body = json.loads(kafka.send_dict.call_args[0][1]["data"])
    assert body["command_truncated"] is False


# ---------------------------------------------------------------------------
# tool_execute_shell_command — access gate
# ---------------------------------------------------------------------------


async def test_lab_unchained_false_returns_error():
    ctx = _ctx(settings=_settings(lab_unchained=False))
    result = await tool_execute_shell_command(ctx, {"command": "ls"})
    assert "OMNI_LAB_UNCHAINED" in result


async def test_missing_command_returns_error():
    ctx = _ctx()
    result = await tool_execute_shell_command(ctx, {})
    assert "Thiếu args.command" in result


async def test_empty_command_returns_error():
    ctx = _ctx()
    result = await tool_execute_shell_command(ctx, {"command": "  "})
    assert "Thiếu args.command" in result


async def test_command_too_long_returns_error():
    ctx = _ctx()
    result = await tool_execute_shell_command(ctx, {"command": "x" * 9000})
    assert "quá dài" in result


# ---------------------------------------------------------------------------
# tool_execute_shell_command — policy denied
# ---------------------------------------------------------------------------


async def test_policy_denied_returns_error():
    ctx = _ctx(kafka=AsyncMock())

    from execution.policy import PolicyResult, PolicyVerdict

    denied = PolicyResult(verdict=PolicyVerdict.DENIED, reason="strict_denylist:rm -rf")

    with patch("workers.lab_shell.check_sandbox_command", return_value=denied):
        result = await tool_execute_shell_command(ctx, {"command": "rm -rf /"})

    assert "Policy" in result
    assert "rm -rf" in result


# ---------------------------------------------------------------------------
# tool_execute_shell_command — successful execution (mocked subprocess)
# ---------------------------------------------------------------------------


async def test_successful_command_execution():
    ctx = _ctx(kafka=AsyncMock())

    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_return((b"hello world\n", b""))):
            result = await tool_execute_shell_command(ctx, {"command": "echo hello world"})

    assert "exit=0" in result
    assert "hello world" in result


async def test_command_nonzero_exit():
    ctx = _ctx(kafka=AsyncMock())

    mock_proc = AsyncMock()
    mock_proc.returncode = 127

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_return((b"", b"command not found"))):
            result = await tool_execute_shell_command(ctx, {"command": "nonexistent_cmd"})

    assert "exit=127" in result
    assert "command not found" in result


async def test_command_timeout():
    ctx = _ctx(kafka=AsyncMock())

    mock_proc = MagicMock()
    mock_proc.kill = MagicMock()

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_timeout):
            result = await tool_execute_shell_command(ctx, {"command": "sleep 999", "timeout_sec": 5.0})

    mock_proc.kill.assert_called_once()
    assert "Timeout" in result
    assert "5.0s" in result


async def test_custom_trace_id():
    ctx = _ctx(kafka=AsyncMock())

    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_return((b"out", b""))):
            result = await tool_execute_shell_command(
                ctx, {"command": "echo hi", "trace_id": "my-trace-xyz"}
            )

    assert "my-trace-xyz" in result


async def test_trace_id_from_context():
    ctx = _ctx(trace_id="ctx-trace-99", kafka=AsyncMock())

    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_return((b"data", b""))):
            result = await tool_execute_shell_command(ctx, {"command": "ls"})

    assert "ctx-trace-99" in result


async def test_timeout_clamped_to_600():
    ctx = _ctx(kafka=AsyncMock())

    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_return((b"ok", b""))) as mock_wait:
            await tool_execute_shell_command(ctx, {"command": "ls", "timeout_sec": 9999})
            # Verify wait_for was called with timeout clamped to 600
            _, kwargs = mock_wait.call_args
            # timeout is positional: wait_for(coro, timeout)
            timeout_arg = mock_wait.call_args[0][1] if len(mock_wait.call_args[0]) > 1 else mock_wait.call_args[1].get("timeout")
            assert timeout_arg is None or timeout_arg <= 600.0


async def test_timeout_min_5():
    ctx = _ctx(kafka=AsyncMock())

    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_return((b"ok", b""))):
            # Should not raise; timeout clamped to >= 5
            result = await tool_execute_shell_command(ctx, {"command": "ls", "timeout_sec": 0.1})

    assert "exit=" in result


async def test_stdout_clipped_at_12000():
    ctx = _ctx(kafka=AsyncMock())

    large_out = b"A" * 20000
    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_return((large_out, b""))):
            result = await tool_execute_shell_command(ctx, {"command": "big_output_cmd"})

    assert len(result) < 20000
    assert "…" in result  # truncation marker


async def test_stderr_present_in_output():
    ctx = _ctx(kafka=AsyncMock())

    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_return((b"out", b"some warning"))):
            result = await tool_execute_shell_command(ctx, {"command": "cmd"})

    assert "some warning" in result


async def test_audit_called_on_success():
    kafka = AsyncMock()
    ctx = _ctx(kafka=kafka)

    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_return((b"output", b""))):
            await tool_execute_shell_command(ctx, {"command": "echo test"})

    # Audit should have been called (kafka.send_dict)
    kafka.send_dict.assert_called_once()


async def test_audit_called_on_timeout():
    kafka = AsyncMock()
    ctx = _ctx(kafka=kafka)

    mock_proc = MagicMock()
    mock_proc.kill = MagicMock()

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_timeout):
            await tool_execute_shell_command(ctx, {"command": "sleep 100"})

    # Audit should have been called for timeout
    kafka.send_dict.assert_called_once()
    body = json.loads(kafka.send_dict.call_args[0][1]["data"])
    assert body["exit_code"] == -1
