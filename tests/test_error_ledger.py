"""ErrorLedger: record via log_error_to_ledger (Postgres/pgvector)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.error_ledger import ErrorLedger
from rag.pgvector_store import COLLECTION_ERRORS


@pytest.mark.asyncio
async def test_ensure_ready_calls_pgvector_store_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = AsyncMock()
    mock_store = MagicMock()
    mock_store.ensure_ready = AsyncMock()

    def _fake_vs(_pool: object) -> MagicMock:
        return mock_store

    monkeypatch.setattr("rag.pgvector_store.PGVectorStore", _fake_vs)
    ledger = ErrorLedger(pool)
    await ledger.ensure_ready()
    mock_store.ensure_ready.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_error_calls_log_error_to_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = AsyncMock()
    log = AsyncMock(return_value="point-id-1")
    monkeypatch.setattr("rag.error_ledger.log_error_to_ledger", log)

    ledger = ErrorLedger(pool)
    pid = await ledger.record_error(
        title="t",
        detail="d",
        phase="p",
        component="c",
        swallow_errors=False,
    )
    assert pid == "point-id-1"
    log.assert_awaited_once()
    assert log.call_args.kwargs["phase"] == "p"
    assert log.call_args.kwargs["component"] == "c"
    assert log.call_args.kwargs["collection_name"] == COLLECTION_ERRORS


@pytest.mark.asyncio
async def test_record_error_swallows_when_backend_down(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = AsyncMock()
    monkeypatch.setattr(
        "rag.error_ledger.log_error_to_ledger",
        AsyncMock(side_effect=ConnectionError("boom")),
    )

    ledger = ErrorLedger(pool)
    out = await ledger.record_error(
        title="t",
        detail="d",
        phase="p",
        swallow_errors=True,
    )
    assert out is None


@pytest.mark.asyncio
async def test_record_exception_includes_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = AsyncMock()
    log = AsyncMock(return_value="pid-2")
    monkeypatch.setattr("rag.error_ledger.log_error_to_ledger", log)

    ledger = ErrorLedger(pool)

    try:
        raise ValueError("bad")
    except ValueError as e:
        await ledger.record_exception(e, phase="2", component="test", swallow_errors=False)

    assert log.await_count == 1
    detail = log.call_args.kwargs["detail"]
    assert "ValueError" in detail
    assert "bad" in detail


@pytest.mark.asyncio
async def test_record_error_redacts_telegram_url_in_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = AsyncMock()
    log = AsyncMock(return_value="pid-3")
    monkeypatch.setattr("rag.error_ledger.log_error_to_ledger", log)

    ledger = ErrorLedger(pool)
    dirty = (
        "url 'https://api.telegram.org/bot123456:SECRETTOKEN/getUpdates?x=1' failed"
    )
    await ledger.record_error(
        title="t",
        detail=dirty,
        phase="p",
        component="c",
        swallow_errors=False,
    )
    passed = log.call_args.kwargs["detail"]
    assert "SECRETTOKEN" not in passed
    assert "bot[REDACTED]" in passed or "[REDACTED]" in passed
