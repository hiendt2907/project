"""Tests for the read-only verification runner (workers.kb_verifier)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from workers.kb_verifier import (
    ProbeResult,
    is_readonly_command,
    run_readonly_verification,
)


class FakeRedis:
    """Minimal async Redis stub sufficient for kb_verifier code paths."""

    def __init__(self, decode_responses: bool = True) -> None:
        self.decode_responses = decode_responses
        self._store: dict[str, str] = {}

    async def get(self, key: str):
        return self._store.get(key)

    async def setex(self, key: str, ttl: int, val: str):
        self._store[key] = val

    async def set(self, key: str, val: str, nx: bool = False, ex: int | None = None):
        self._store[key] = val
        return True


def _step(command: str, layer: str) -> SimpleNamespace:
    return SimpleNamespace(command=command, layer=layer)


# --- is_readonly_command ------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "kubectl get pods",
        "kubectl describe pod x",
        "top -b -n1",
        "df -hT",
    ],
)
def test_is_readonly_allows_safe_commands(cmd):
    assert is_readonly_command(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "kubectl delete pod x",
        "kubectl rollout restart deploy/x",
        "kubectl exec -it pod -- sh",
        "rm -rf /",
        "kubectl get pods; rm x",
    ],
)
def test_is_readonly_blocks_mutating_commands(cmd):
    assert is_readonly_command(cmd) is False


# --- run_readonly_verification ------------------------------------------------


def _ctx():
    return SimpleNamespace(redis=FakeRedis(decode_responses=True), settings=SimpleNamespace())


@pytest.mark.asyncio
async def test_mutating_steps_are_blocked_not_run():
    advisory = SimpleNamespace(
        verification_steps=[
            _step("kubectl delete pod x", "kubernetes"),
            _step("kubectl rollout restart deploy/x", "kubernetes"),
            _step("kubectl get pods; rm x", "kubernetes"),
        ]
    )
    results = await run_readonly_verification(
        _ctx(), advisory=advisory, trace="t-1", max_probes=4
    )
    assert len(results) == 3
    for r in results:
        assert isinstance(r, ProbeResult)
        assert r.blocked is True
        assert r.ran is False
        assert r.rc == -1
        assert r.error == "not in read-only allowlist"


@pytest.mark.asyncio
async def test_allowed_host_command_degrades_without_executor():
    # df has no in-process async executor → degraded, not blocked.
    advisory = SimpleNamespace(verification_steps=[_step("df -hT", "os_baremetal")])
    results = await run_readonly_verification(
        _ctx(), advisory=advisory, trace="t-2", max_probes=4
    )
    assert len(results) == 1
    r = results[0]
    assert r.blocked is False
    assert r.ran is False
    assert r.error == "no read-only executor available"


@pytest.mark.asyncio
async def test_max_probes_caps_step_count():
    steps = [_step("kubectl delete pod x", "kubernetes") for _ in range(10)]
    advisory = SimpleNamespace(verification_steps=steps)
    results = await run_readonly_verification(
        _ctx(), advisory=advisory, trace="t-3", max_probes=2
    )
    assert len(results) == 2


@pytest.mark.asyncio
async def test_empty_advisory_returns_empty_and_does_not_raise():
    advisory = SimpleNamespace(verification_steps=[])
    results = await run_readonly_verification(
        _ctx(), advisory=advisory, trace="t-4", max_probes=4
    )
    assert results == []


@pytest.mark.asyncio
async def test_kubectl_get_pods_attempts_executor():
    # Allowlisted + mapped to a tool. With no real cluster the tool returns an
    # error string (caught internally) → ran=True with an error-ish payload, or
    # blocked stays False. Either way it must not raise and must not be blocked.
    advisory = SimpleNamespace(
        verification_steps=[_step("kubectl get pods -n multi-agent", "kubernetes")]
    )
    results = await run_readonly_verification(
        _ctx(), advisory=advisory, trace="t-5", max_probes=1
    )
    assert len(results) == 1
    assert results[0].blocked is False
