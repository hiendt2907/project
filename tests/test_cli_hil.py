"""cli_hil pools + ingest (dry-run, payload)."""

from __future__ import annotations

import pytest

from rag.pgvector_store import COLLECTION_CLI_HIL_CONTEXT
from training.cli_hil_pools import (
    CLI_SUGGEST_ALLOWLIST,
    MAX_COMBINATIONS,
    generate_cli_hil_entry,
    cli_hil_point_id,
)
from training.cli_hil_ingest import build_run_commands_json, run_cli_hil_ingest
from workers.settings import WorkerSettings


def test_cli_hil_generates_100k_indices() -> None:
    a = generate_cli_hil_entry(0)
    b = generate_cli_hil_entry(MAX_COMBINATIONS - 1)
    assert a.index == 0
    assert b.index == MAX_COMBINATIONS - 1
    assert a.point_id != b.point_id
    assert len(a.suggested_commands) == 2
    assert a.suggested_commands[0] in CLI_SUGGEST_ALLOWLIST


def test_cli_hil_stable_point_id() -> None:
    assert cli_hil_point_id(42) == cli_hil_point_id(42)


@pytest.mark.asyncio
async def test_cli_hil_dry_run_count() -> None:
    ws = WorkerSettings()
    n = await run_cli_hil_ingest(
        settings=ws,
        count=12,
        dry_run=True,
        checkpoint_path=None,
        start_index=0,
        collection=COLLECTION_CLI_HIL_CONTEXT,
        embed_batch=8,
        upsert_batch=64,
        log_every=100,
    )
    assert n == 12


@pytest.mark.asyncio
async def test_cli_hil_dry_run_slice_end() -> None:
    """begin+count vượt MAX → cắt ở MAX_COMBINATIONS."""
    ws = WorkerSettings()
    n = await run_cli_hil_ingest(
        settings=ws,
        count=50,
        dry_run=True,
        checkpoint_path=None,
        start_index=MAX_COMBINATIONS - 10,
        collection=COLLECTION_CLI_HIL_CONTEXT,
        embed_batch=8,
        upsert_batch=64,
        log_every=100,
    )
    assert n == 10


def test_build_run_commands_json_shape() -> None:
    ws = WorkerSettings()
    out = build_run_commands_json(
        settings=ws,
        collection="cli_hil_context",
        count=100_000,
        dry_run=False,
        checkpoint="/tmp/cp.json",
    )
    assert "ingest_summary" in out and "commands" in out
    assert out["ingest_summary"]["points_target"] == 100_000
    assert isinstance(out["commands"], list)
    assert any("cli_hil_ingest" in c for c in out["commands"])
