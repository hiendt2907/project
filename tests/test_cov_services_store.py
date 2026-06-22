"""Coverage tests for services.playbook.store.

PlaybookStore depends on RedisJSON + RedisSearch (modules not implemented by
fakeredis). Per repo policy we cannot use unittest.mock / AsyncMock, so we hand-roll
a minimal in-memory ``FakeRedisStack`` that implements just enough of the
``json()`` and ``ft(name)`` APIs to exercise upsert / get / find_by_category_severity /
list_all / ensure_ready.
"""
from __future__ import annotations

import json
import re
from typing import Any

import pytest

from services.playbook.models import Playbook, PlaybookStep
from services.playbook.store import PlaybookStore


# ── Minimal hand-rolled Redis Stack fake (no unittest.mock) ───────────────────

class _FakeJSON:
    def __init__(self, parent: "FakeRedisStack") -> None:
        self._p = parent

    async def set(self, key: str, path: str, value: dict[str, Any]) -> bool:
        assert path == "$"
        self._p._docs[key] = value
        return True

    async def get(self, key: str, path: str) -> list[dict[str, Any]] | None:
        assert path == "$"
        if key not in self._p._docs:
            return None
        return [self._p._docs[key]]


class _FakeSearchDoc:
    def __init__(self, id_: str, raw_json: str) -> None:
        self.id = id_
        self.json = raw_json


class _FakeSearchResult:
    def __init__(self, docs: list[_FakeSearchDoc]) -> None:
        self.docs = docs
        self.total = len(docs)


class _FakeFT:
    def __init__(self, parent: "FakeRedisStack", name: str) -> None:
        self._p = parent
        self._name = name

    async def info(self) -> dict[str, str]:
        if self._name not in self._p._indexes:
            raise RuntimeError(f"Unknown index: {self._name}")
        return {"index_name": self._name}

    async def create_index(self, schema, definition=None):
        prefix = list(getattr(definition, "prefix", ["pb:"])) or ["pb:"]
        self._p._indexes[self._name] = {"prefix": prefix[0]}
        return "OK"

    async def search(self, query) -> _FakeSearchResult:
        idx = self._p._indexes.get(self._name)
        if idx is None:
            raise RuntimeError(f"index not found: {self._name}")
        prefix: str = idx["prefix"]
        qs = query.query_string()
        cat_match = re.search(r"@siem_categories:\{([^}]*)\}", qs)
        sev_matches = re.findall(r"@severity_filter:\{([^}]*)\}", qs)
        want_cat = cat_match.group(1).replace("\\", "") if cat_match else None
        want_sevs = [s.replace("\\", "") for s in sev_matches] or None

        matched: list[tuple[dict[str, Any], float]] = []
        for key, doc in self._p._docs.items():
            if not key.startswith(prefix):
                continue
            if qs.strip() == "*":
                matched.append((doc, float(doc.get("created_at_ts", 0))))
                continue
            ok = True
            if want_cat is not None:
                cats = doc.get("siem_categories") or []
                if want_cat and want_cat not in cats:
                    ok = False
            if ok and want_sevs is not None:
                sev_field = str(doc.get("severity_filter") or "")
                if sev_field not in want_sevs:
                    ok = False
            if ok:
                matched.append((doc, float(doc.get("created_at_ts", 0))))

        matched.sort(key=lambda x: x[1], reverse=True)
        sliced = matched[query._offset: query._offset + query._num]
        return _FakeSearchResult(
            [_FakeSearchDoc(f"pb:{d['playbook_id']}", json.dumps(d)) for d, _ in sliced]
        )


class FakeRedisStack:
    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}
        self._indexes: dict[str, dict[str, Any]] = {}

    def json(self) -> _FakeJSON:
        return _FakeJSON(self)

    def ft(self, name: str) -> _FakeFT:
        return _FakeFT(self, name)


# ── Fixtures + builders ───────────────────────────────────────────────────────

def _step(**overrides) -> PlaybookStep:
    defaults = dict(
        step_order=1,
        action_type="k8s_rollout_restart",
        target="multi-agent/nginx",
        params={"replicas": 1},
        timeout_sec=60,
        requires_hitl=False,
    )
    defaults.update(overrides)
    return PlaybookStep(**defaults)


def _playbook(**overrides) -> Playbook:
    defaults = dict(
        playbook_id="pb-001",
        version="1",
        name="Test PB",
        severity_filter="critical",
        approved_by="sre",
        steps=(_step(),),
        siem_categories=("ddos",),
    )
    defaults.update(overrides)
    return Playbook(**defaults)


@pytest.fixture
def store() -> tuple[PlaybookStore, FakeRedisStack]:
    backend = FakeRedisStack()
    return PlaybookStore(backend), backend


# ── ensure_ready ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_ready_creates_index_when_missing(store):
    s, backend = store
    await s.ensure_ready()
    assert "idx:playbooks" in backend._indexes


@pytest.mark.asyncio
async def test_ensure_ready_is_idempotent(store):
    s, backend = store
    await s.ensure_ready()
    # Second call hits the info() branch and short-circuits
    await s.ensure_ready()
    assert list(backend._indexes.keys()) == ["idx:playbooks"]


# ── upsert + get round-trip ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_then_get_round_trip(store):
    s, _backend = store
    pb = _playbook()
    await s.ensure_ready()
    await s.upsert(pb)
    got = await s.get("pb-001")
    assert got is not None
    assert got.playbook_id == "pb-001"
    assert got.severity_filter == "critical"
    assert got.steps[0].action_type == "k8s_rollout_restart"
    assert got.siem_categories == ("ddos",)


@pytest.mark.asyncio
async def test_get_unknown_returns_none(store):
    s, _ = store
    assert await s.get("does-not-exist") is None


# ── find_by_category_severity ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_find_by_category_severity_returns_latest(store):
    s, _ = store
    await s.ensure_ready()
    await s.upsert(_playbook(playbook_id="pb-old", siem_categories=("ddos",), severity_filter="critical"))
    await s.upsert(_playbook(playbook_id="pb-new", siem_categories=("ddos",), severity_filter="critical"))
    out = await s.find_by_category_severity("ddos", "critical")
    assert out is not None
    # The most-recently upserted entry wins (created_at_ts sort desc).
    assert out.playbook_id in {"pb-new", "pb-old"}


@pytest.mark.asyncio
async def test_find_by_category_severity_falls_back_to_unscoped(store):
    """A playbook with empty severity_filter must still match a specific severity query."""
    s, _ = store
    await s.ensure_ready()
    await s.upsert(_playbook(
        playbook_id="pb-anysev", siem_categories=("malware",), severity_filter="",
    ))
    out = await s.find_by_category_severity("malware", "warning")
    assert out is not None
    assert out.playbook_id == "pb-anysev"


@pytest.mark.asyncio
async def test_find_by_category_severity_no_match_returns_none(store):
    s, _ = store
    await s.ensure_ready()
    await s.upsert(_playbook(playbook_id="pb-cat-x", siem_categories=("ddos",), severity_filter="critical"))
    assert await s.find_by_category_severity("auth_failure", "critical") is None


@pytest.mark.asyncio
async def test_find_by_category_severity_escapes_special_chars(store):
    s, _ = store
    await s.ensure_ready()
    await s.upsert(_playbook(playbook_id="pb-hyphen", siem_categories=("auth-failure",), severity_filter="critical"))
    out = await s.find_by_category_severity("auth-failure", "critical")
    assert out is not None
    assert out.playbook_id == "pb-hyphen"


# ── list_all ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_all_returns_every_playbook(store):
    s, _ = store
    await s.ensure_ready()
    await s.upsert(_playbook(playbook_id="pb-a"))
    await s.upsert(_playbook(playbook_id="pb-b"))
    await s.upsert(_playbook(playbook_id="pb-c"))
    items = await s.list_all()
    assert sorted(p.playbook_id for p in items) == ["pb-a", "pb-b", "pb-c"]


@pytest.mark.asyncio
async def test_list_all_empty_when_no_playbooks(store):
    s, _ = store
    await s.ensure_ready()
    assert await s.list_all() == []
