"""PlaybookStore tests with mocked Redis Stack / FT APIs."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.playbook.models import Playbook, PlaybookStep
from services.playbook.store import PlaybookStore


def _sample_playbook() -> Playbook:
    return Playbook(
        playbook_id="pb-1",
        version="1",
        name="Test",
        severity_filter="critical",
        approved_by="qa",
        steps=(
            PlaybookStep(
                step_order=1,
                action_type="SCALE_DEPLOYMENT",
                target="ns/deploy",
                params={"replicas": 2},
                timeout_sec=120,
                requires_hitl=True,
            ),
        ),
        siem_categories=("ddos",),
    )


@pytest.mark.asyncio
async def test_ensure_ready_index_exists() -> None:
    r = MagicMock()
    r.ft.return_value.info = AsyncMock(return_value={"num_docs": 1})
    store = PlaybookStore(r)
    await store.ensure_ready()
    r.ft.assert_called_once()
    r.ft.return_value.create_index.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_ready_creates_index_on_missing() -> None:
    r = MagicMock()
    r.ft.return_value.info = AsyncMock(side_effect=Exception("no such index"))
    r.ft.return_value.create_index = AsyncMock()
    store = PlaybookStore(r)
    await store.ensure_ready()
    r.ft.return_value.create_index.assert_called_once()


@pytest.mark.asyncio
async def test_get_returns_none_when_missing() -> None:
    r = MagicMock()
    r.json.return_value.get = AsyncMock(return_value=None)
    store = PlaybookStore(r)
    assert await store.get("missing") is None


@pytest.mark.asyncio
async def test_get_returns_none_for_empty_doc_list() -> None:
    r = MagicMock()
    r.json.return_value.get = AsyncMock(return_value=[])
    store = PlaybookStore(r)
    assert await store.get("pb") is None


@pytest.mark.asyncio
async def test_get_parses_single_element_list() -> None:
    doc = {
        "playbook_id": "pb-1",
        "version": "1",
        "name": "N",
        "severity_filter": "high",
        "approved_by": "a",
        "siem_categories": ["malware"],
        "steps": [
            {
                "step_order": 1,
                "action_type": "PATCH",
                "target": "t",
                "params": {"k": "v"},
                "timeout_sec": 30,
                "requires_hitl": False,
            }
        ],
    }
    r = MagicMock()
    r.json.return_value.get = AsyncMock(return_value=[doc])
    store = PlaybookStore(r)
    pb = await store.get("pb-1")
    assert pb is not None
    assert pb.playbook_id == "pb-1"
    assert pb.steps[0].params == {"k": "v"}


@pytest.mark.asyncio
async def test_find_by_category_severity_none_when_no_docs() -> None:
    r = MagicMock()
    results = MagicMock()
    results.docs = []
    r.ft.return_value.search = AsyncMock(return_value=results)
    store = PlaybookStore(r)
    assert await store.find_by_category_severity("x", "critical") is None


@pytest.mark.asyncio
async def test_find_by_category_severity_json_list_wrapper() -> None:
    inner = {
        "playbook_id": "pb-2",
        "version": "1",
        "name": "Wrap",
        "severity_filter": "",
        "approved_by": "b",
        "siem_categories": ["k8s_threat"],
        "steps": [],
    }
    r = MagicMock()
    results = MagicMock()
    doc = MagicMock()
    doc.json = json.dumps([inner])
    results.docs = [doc]
    r.ft.return_value.search = AsyncMock(return_value=results)
    store = PlaybookStore(r)
    pb = await store.find_by_category_severity("k8s_threat", "critical")
    assert pb is not None
    assert pb.playbook_id == "pb-2"


@pytest.mark.asyncio
async def test_list_all_unpacks_list_json() -> None:
    inner = {
        "playbook_id": "pb-3",
        "version": "1",
        "name": "L",
        "severity_filter": "info",
        "approved_by": "c",
        "siem_categories": [],
        "steps": [],
    }
    r = MagicMock()
    results = MagicMock()
    doc = MagicMock()
    doc.json = [inner]
    results.docs = [doc]
    r.ft.return_value.search = AsyncMock(return_value=results)
    store = PlaybookStore(r)
    pbs = await store.list_all()
    assert len(pbs) == 1
    assert pbs[0].playbook_id == "pb-3"


@pytest.mark.asyncio
async def test_upsert_writes_json() -> None:
    r = MagicMock()
    r.json.return_value.set = AsyncMock()
    store = PlaybookStore(r)
    await store.upsert(_sample_playbook())
    r.json.return_value.set.assert_called_once()
    key, path, doc = r.json.return_value.set.call_args[0]
    assert key == "pb:pb-1"
    assert path == "$"
    assert doc["playbook_id"] == "pb-1"
    assert len(doc["steps"]) == 1
