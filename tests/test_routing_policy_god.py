"""God mode fast-path allowlist."""

from __future__ import annotations

from workers.routing_policy import (
    GOD_MODE_FAST_PATH_EXTRA_TOOLS,
    fast_path_auto_execute_allowlist,
    is_fast_path_auto_allowed,
    shell_fast_path_enabled,
)
from workers.settings import WorkerSettings


def test_shell_fast_path_enabled() -> None:
    assert shell_fast_path_enabled(None) is False
    assert shell_fast_path_enabled(WorkerSettings()) is False
    assert shell_fast_path_enabled(WorkerSettings(god_mode=True)) is True
    assert shell_fast_path_enabled(WorkerSettings(lab_unchained=True)) is True


def test_is_fast_path_auto_allowed_execute_shell_god_only() -> None:
    assert is_fast_path_auto_allowed("execute_shell_command", None) is False
    assert is_fast_path_auto_allowed("execute_shell_command", WorkerSettings()) is False
    assert is_fast_path_auto_allowed("execute_shell_command", WorkerSettings(god_mode=True)) is True
    assert is_fast_path_auto_allowed("echo", WorkerSettings()) is True
    assert is_fast_path_auto_allowed("k8s_rollout_restart", WorkerSettings(god_mode=True)) is False


def test_fast_path_allowlist_union() -> None:
    base = fast_path_auto_execute_allowlist(WorkerSettings())
    god = fast_path_auto_execute_allowlist(WorkerSettings(god_mode=True))
    assert "echo" in base and "echo" in god
    assert "execute_shell_command" not in base
    assert "execute_shell_command" in god
    assert GOD_MODE_FAST_PATH_EXTRA_TOOLS <= god
