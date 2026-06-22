"""Coverage tests for src/services/playbook — models, matcher, store helpers."""
from __future__ import annotations

import json
import pytest
import fakeredis.aioredis
from unittest.mock import AsyncMock, MagicMock

from services.playbook.models import Playbook, PlaybookStep
from services.playbook.matcher import PlaybookMatcher
from services.playbook.store import PlaybookStore, _doc_to_playbook, _escape_tag


# ── PlaybookStep & Playbook models ────────────────────────────────────────────

def _make_step(**overrides) -> PlaybookStep:
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


def _make_playbook(**overrides) -> Playbook:
    step = _make_step()
    defaults = dict(
        playbook_id="pb-001",
        version="1",
        name="Test Playbook",
        severity_filter="critical",
        approved_by="sre-team",
        steps=(step,),
        siem_categories=("ddos",),
    )
    defaults.update(overrides)
    return Playbook(**defaults)


def test_playbook_step_is_frozen():
    step = _make_step()
    with pytest.raises(Exception):
        step.step_order = 99  # type: ignore[misc]


def test_playbook_is_frozen():
    pb = _make_playbook()
    with pytest.raises(Exception):
        pb.playbook_id = "pb-999"  # type: ignore[misc]


def test_playbook_step_lookup_by_order():
    step1 = _make_step(step_order=1)
    step2 = _make_step(step_order=2, action_type="k8s_scale_deployment")
    pb = _make_playbook(steps=(step1, step2))
    assert pb.step(1) is step1
    assert pb.step(2) is step2
    assert pb.step(99) is None


def test_playbook_first_step_returns_min_order():
    step2 = _make_step(step_order=2)
    step1 = _make_step(step_order=1)
    pb = _make_playbook(steps=(step2, step1))
    assert pb.first_step() is step1


def test_playbook_first_step_none_when_empty():
    pb = _make_playbook(steps=())
    assert pb.first_step() is None


def test_playbook_siem_categories_default_empty():
    step = _make_step()
    pb = Playbook(
        playbook_id="pb-002",
        version="1",
        name="No SIEM",
        severity_filter="",
        approved_by="sre",
        steps=(step,),
    )
    assert pb.siem_categories == ()


# ── _escape_tag ────────────────────────────────────────────────────────────────

def test_escape_tag_clean_string():
    result = _escape_tag("ddos")
    assert result == "ddos"


def test_escape_tag_hyphen_escaped():
    result = _escape_tag("auth-failure")
    assert "\\-" in result


def test_escape_tag_at_sign_escaped():
    result = _escape_tag("foo@bar")
    assert "\\@" in result


def test_escape_tag_empty():
    assert _escape_tag("") == ""


def test_escape_tag_spaces_escaped():
    result = _escape_tag("k8s threat")
    assert "\\ " in result


# ── _doc_to_playbook ──────────────────────────────────────────────────────────

def test_doc_to_playbook_basic():
    doc = {
        "playbook_id": "pb-010",
        "version": "2",
        "name": "Test PB",
        "severity_filter": "critical",
        "approved_by": "ops",
        "siem_categories": ["ddos", "malware"],
        "steps": [
            {
                "step_order": 1,
                "action_type": "k8s_rollout_restart",
                "target": "ns/deploy",
                "params": {"replicas": 0},
                "timeout_sec": 30,
                "requires_hitl": False,
            }
        ],
    }
    pb = _doc_to_playbook(doc)
    assert pb.playbook_id == "pb-010"
    assert pb.version == "2"
    assert len(pb.steps) == 1
    assert pb.steps[0].step_order == 1
    assert "ddos" in pb.siem_categories


def test_doc_to_playbook_no_steps():
    doc = {
        "playbook_id": "pb-011",
        "version": "1",
        "name": "Empty",
        "severity_filter": "",
        "approved_by": "",
        "siem_categories": [],
        "steps": [],
    }
    pb = _doc_to_playbook(doc)
    assert len(pb.steps) == 0


def test_doc_to_playbook_params_as_json_string():
    doc = {
        "playbook_id": "pb-012",
        "version": "1",
        "name": "Json params",
        "severity_filter": "warning",
        "approved_by": "ops",
        "siem_categories": None,
        "steps": [
            {
                "step_order": 1,
                "action_type": "tool",
                "target": "ns/pod",
                "params": '{"key": "value"}',  # JSON string instead of dict
                "timeout_sec": 60,
                "requires_hitl": True,
            }
        ],
    }
    pb = _doc_to_playbook(doc)
    assert pb.steps[0].params == {"key": "value"}
    assert pb.steps[0].requires_hitl is True


def test_doc_to_playbook_missing_optional_fields():
    # Minimal doc — no version, name, etc.
    doc = {
        "playbook_id": "pb-013",
        "steps": [],
    }
    pb = _doc_to_playbook(doc)
    assert pb.playbook_id == "pb-013"
    assert pb.version == "1"
    assert pb.name == ""


# ── PlaybookMatcher ────────────────────────────────────────────────────────────

@pytest.fixture
async def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _mock_store() -> MagicMock:
    store = MagicMock()
    store.get = AsyncMock(return_value=None)
    store.find_by_category_severity = AsyncMock(return_value=None)
    return store


@pytest.mark.asyncio
async def test_matcher_by_id_found():
    pb = _make_playbook()
    store = _mock_store()
    store.get = AsyncMock(return_value=pb)
    matcher = PlaybookMatcher(store)
    result = await matcher.match(playbook_id="pb-001")
    assert result is pb


@pytest.mark.asyncio
async def test_matcher_by_id_not_found_falls_to_category():
    pb = _make_playbook()
    store = _mock_store()
    store.get = AsyncMock(return_value=None)
    store.find_by_category_severity = AsyncMock(return_value=pb)
    matcher = PlaybookMatcher(store)
    result = await matcher.match(playbook_id="pb-missing", siem_category="ddos", severity="critical")
    assert result is pb


@pytest.mark.asyncio
async def test_matcher_no_id_uses_category():
    pb = _make_playbook()
    store = _mock_store()
    store.find_by_category_severity = AsyncMock(return_value=pb)
    matcher = PlaybookMatcher(store)
    result = await matcher.match(siem_category="ddos", severity="critical")
    assert result is pb
    store.get.assert_not_called()


@pytest.mark.asyncio
async def test_matcher_no_match_returns_none():
    store = _mock_store()
    matcher = PlaybookMatcher(store)
    result = await matcher.match(siem_category="unknown_category")
    assert result is None


@pytest.mark.asyncio
async def test_matcher_empty_args_returns_none():
    store = _mock_store()
    matcher = PlaybookMatcher(store)
    result = await matcher.match()
    assert result is None


@pytest.mark.asyncio
async def test_matcher_from_batch_with_finguard_labels():
    pb = _make_playbook()
    store = _mock_store()
    store.find_by_category_severity = AsyncMock(return_value=pb)
    matcher = PlaybookMatcher(store)

    batch = [
        {
            "canonical_query_snippet": json.dumps({
                "labels": {
                    "siem_source": "finguard",
                    "siem_category": "ddos",
                    "severity": "critical",
                    "siem_playbook_id": "",
                }
            })
        }
    ]
    result = await matcher.match_from_batch(batch)
    assert result is pb


@pytest.mark.asyncio
async def test_matcher_from_batch_non_json_snippet():
    store = _mock_store()
    matcher = PlaybookMatcher(store)
    batch = [{"canonical_query_snippet": "not json at all"}]
    result = await matcher.match_from_batch(batch)
    assert result is None


@pytest.mark.asyncio
async def test_matcher_from_batch_wrong_siem_source():
    store = _mock_store()
    matcher = PlaybookMatcher(store)
    batch = [
        {
            "canonical_query_snippet": json.dumps({
                "labels": {
                    "siem_source": "prometheus",  # not finguard
                    "siem_category": "ddos",
                    "severity": "critical",
                }
            })
        }
    ]
    result = await matcher.match_from_batch(batch)
    assert result is None


@pytest.mark.asyncio
async def test_matcher_from_batch_empty_batch():
    store = _mock_store()
    matcher = PlaybookMatcher(store)
    result = await matcher.match_from_batch([])
    assert result is None


@pytest.mark.asyncio
async def test_matcher_from_batch_with_playbook_id():
    pb = _make_playbook()
    store = _mock_store()
    store.get = AsyncMock(return_value=pb)
    matcher = PlaybookMatcher(store)

    batch = [
        {
            "canonical_query_snippet": json.dumps({
                "labels": {
                    "siem_source": "finguard",
                    "siem_category": "ddos",
                    "severity": "critical",
                    "siem_playbook_id": "pb-001",
                }
            })
        }
    ]
    result = await matcher.match_from_batch(batch)
    assert result is pb


@pytest.mark.asyncio
async def test_matcher_from_batch_bad_json():
    store = _mock_store()
    matcher = PlaybookMatcher(store)
    batch = [{"canonical_query_snippet": "{bad json"}]
    result = await matcher.match_from_batch(batch)
    assert result is None


# ── PlaybookStore internal helpers (no Redis required) ────────────────────────

def test_playbook_step_requires_hitl_false_by_default():
    step = _make_step(requires_hitl=False)
    assert step.requires_hitl is False


def test_playbook_step_requires_hitl_true():
    step = _make_step(requires_hitl=True)
    assert step.requires_hitl is True


def test_doc_to_playbook_siem_categories_none():
    doc = {
        "playbook_id": "pb-014",
        "steps": [],
        "siem_categories": None,
    }
    pb = _doc_to_playbook(doc)
    assert pb.siem_categories == ()
