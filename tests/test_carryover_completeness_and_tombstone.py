"""F1-CP1 + F18-followup carry-over fixes.

F1-CP1: alert thiếu pod label trên alert pod-scoped → flag completeness (inferred/incomplete).
F18-followup: action auto-rolled-back → trace terminal, KHÔNG re-evaluate (tránh loop
snapshot→settle→rollback do analyst re-plan + re-publish EXECUTE_MUTATE cùng trace).
"""
import json
from types import SimpleNamespace

import pytest

from workers.omni_worker import _alert_completeness_flag


def _alert(labels: dict) -> dict:
    return {"source": "prometheus", "data": {"alerts": [{"labels": labels}]}}


# ── F1-CP1 ───────────────────────────────────────────────────────────────────
def test_explicit_pod_label_is_complete():
    assert _alert_completeness_flag(_alert({"alertname": "OOMKilled", "pod": "nginx-x", "namespace": "ns"})) is None


def test_missing_pod_with_workload_is_inferred():
    lvl, det = _alert_completeness_flag(_alert({"alertname": "OOMKilled", "deployment": "nginx", "namespace": "ns"}))
    assert lvl == "inferred"
    assert "nginx" in det


def test_missing_pod_and_workload_is_incomplete():
    lvl, det = _alert_completeness_flag(_alert({"alertname": "KubePodNotReady", "namespace": "ns"}))
    assert lvl == "incomplete"
    assert "inferred downstream" in det


def test_non_pod_scoped_alert_not_flagged():
    assert _alert_completeness_flag(_alert({"alertname": "HighCPUUsage", "namespace": "ns"})) is None


# ── F18-followup ─────────────────────────────────────────────────────────────
class _FakeRedis:
    def __init__(self, terminal: bool = False):
        self._terminal = terminal
        self.sets: dict = {}

    async def get(self, k):
        if k.endswith(":terminal:" ) or "terminal:" in k:
            return b"1" if self._terminal else None
        return None

    async def setex(self, k, ttl, v):
        self.sets[k] = v


@pytest.mark.asyncio
async def test_rolled_back_feedback_is_terminal_no_reevaluate(monkeypatch):
    """status=rolled_back → emit tombstone + return BEFORE re-evaluation transitions."""
    import workers.autonomous_feedback_loop as fl

    tombstones = []
    transitions = []

    async def _fake_tombstone(ctx, **kw):
        tombstones.append(kw)

    async def _fake_transition(ctx, **kw):
        transitions.append(kw.get("transition"))

    async def _fake_mark_stage(*a, **k):
        pass

    monkeypatch.setattr(fl, "emit_terminal_tombstone", _fake_tombstone)
    monkeypatch.setattr(fl, "emit_transition", _fake_transition)
    monkeypatch.setattr(fl, "mark_stage", _fake_mark_stage)

    ctx = SimpleNamespace(redis=_FakeRedis(terminal=False), settings=SimpleNamespace())
    body = {"trace_id": "t-rb", "status": "rolled_back", "exit_code": 1}
    await fl.handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

    assert len(tombstones) == 1
    assert tombstones[0]["reason_code"] == "auto_rollback_terminal"
    # must NOT have started re-evaluation
    assert "TRANSITION_RE_EVALUATED" not in [str(t) for t in transitions]


@pytest.mark.asyncio
async def test_already_terminal_trace_skips_silently(monkeypatch):
    import workers.autonomous_feedback_loop as fl

    tombstones = []

    async def _fake_tombstone(ctx, **kw):
        tombstones.append(kw)

    async def _noop(*a, **k):
        pass

    monkeypatch.setattr(fl, "emit_terminal_tombstone", _fake_tombstone)
    monkeypatch.setattr(fl, "emit_transition", _noop)
    monkeypatch.setattr(fl, "mark_stage", _noop)

    ctx = SimpleNamespace(redis=_FakeRedis(terminal=True), settings=SimpleNamespace())
    body = {"trace_id": "t-done", "status": "ok", "exit_code": 0}
    await fl.handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

    # already terminal → no new tombstone, just early return
    assert tombstones == []
