"""Strict sandbox denylist."""

from __future__ import annotations

import pytest

from execution.policy import PolicyVerdict, check_promotion_tool, check_sandbox_command


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /",
        "RM -rf /tmp",
        "rm --no-preserve-root -rf /",
        "mkfs.ext4 /dev/sdb1",
        "dd if=/dev/zero of=/tmp/x bs=1M count=1",
        "echo x > /dev/sda",
        "cat y > /dev/nvme0n1",
        "z > /dev/vdb",
    ],
)
def test_sandbox_denylist_blocks(cmd: str) -> None:
    r = check_sandbox_command(cmd)
    assert r.verdict == PolicyVerdict.DENIED


def test_sandbox_allows_echo() -> None:
    r = check_sandbox_command("echo hello")
    assert r.verdict == PolicyVerdict.ALLOWED_AUTO


def test_promotion_allowlist() -> None:
    assert check_promotion_tool("k8s_rollout_restart").verdict == PolicyVerdict.ALLOWED_AUTO
    assert check_promotion_tool("rm").verdict == PolicyVerdict.DENIED
    assert check_promotion_tool("k8s_scale_deployment").verdict == PolicyVerdict.DENIED


def test_promotion_cluster_full_access() -> None:
    assert (
        check_promotion_tool("k8s_scale_deployment", cluster_full_access=True).verdict
        == PolicyVerdict.ALLOWED_AUTO
    )
    assert (
        check_promotion_tool("kubectl_cluster", cluster_full_access=True).verdict
        == PolicyVerdict.ALLOWED_AUTO
    )
    assert check_promotion_tool("kubectl_cluster", cluster_full_access=False).verdict == PolicyVerdict.DENIED
