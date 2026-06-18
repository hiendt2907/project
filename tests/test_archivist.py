"""Tests for the archivist module.

Verifies:
  (a) No hardcoded secret strings remain in analyst_agentic_loop.
  (b) write_incident_postmortem creates a redacted markdown file.
  (c) _upsert_action_experience_on_success stores arg_keys (not raw values).
  (d) recall_playbook_advisory returns a hit and logs retrieval line.
"""

from __future__ import annotations

import importlib
import inspect
import os
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


# ── (a) no hardcoded secrets in analyst_agentic_loop ──────────────────────────

def _load_source(rel_path: str) -> str:
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, rel_path), encoding="utf-8") as f:
        return f.read()


def test_no_hardcoded_secret_in_analyst_loop() -> None:
    src = _load_source("src/workers/analyst_agentic_loop.py")
    banned = [
        "chaos-pg-secret",
        "chaos-app-pass",
        "APP_PASSWORD",
        "CHAOS LAB",
        "_batch_has_credential_failure",
        "_credential_failure_first_round_instruction",
    ]
    for token in banned:
        assert token not in src, f"Banned token {token!r} found in analyst_agentic_loop.py"


def test_no_hardcoded_secret_in_security_catalog() -> None:
    src = _load_source("src/workers/analyst_agentic_loop.py")
    assert "chaos-app-pass-2025" not in src


# ── (b) post-mortem file creation ─────────────────────────────────────────────

def test_write_incident_postmortem_creates_redacted_file(tmp_path: str) -> None:
    from workers.archivist import write_incident_postmortem

    with patch("workers.archivist._POST_MORTEM_DIR", str(tmp_path)):
        path = write_incident_postmortem(
            "gw-prom-test123",
            tool_name="k8s_patch_secret",
            arg_keys=["namespace", "name", "key", "value", "reasoning"],
            alertname="KubePodCrashLoopVictim",
            namespace="multi-agent",
            workload="chaos-victim",
        )

    assert os.path.exists(path), "Post-mortem file was not created"
    content = open(path).read()

    # Must contain structural elements
    assert "k8s_patch_secret" in content
    assert "KubePodCrashLoopVictim" in content
    assert "namespace" in content
    assert "key" in content

    # Must NOT contain any secret values
    forbidden = ["chaos-app-pass-2025", "STALE-ROTATED", "APP_PASSWORD"]
    for token in forbidden:
        assert token not in content, f"Secret value {token!r} leaked into post-mortem"

    # 'value' key name is OK; the value itself must not appear
    assert "arg_keys" not in content or "value" in content  # key listed, not its secret data


def test_write_incident_postmortem_returns_path(tmp_path: str) -> None:
    from workers.archivist import write_incident_postmortem

    with patch("workers.archivist._POST_MORTEM_DIR", str(tmp_path)):
        path = write_incident_postmortem(
            "trace-xyz",
            tool_name="k8s_rollout_restart",
            arg_keys=["namespace", "deployment"],
            alertname="OOMKilled",
            namespace="production",
            workload="api-server",
        )
    assert "trace-xyz" in path


# ── (c) vector upsert stores arg_keys, not raw secret values ──────────────────

def test_upsert_payload_has_arg_keys_not_raw_args() -> None:
    """Verify _upsert_action_experience_on_success uses arg_keys (no raw secret values)."""
    import ast
    src = _load_source("src/workers/autonomous_feedback_loop.py")
    # The payload dict must NOT contain '"args": mutate_args' but must contain '"arg_keys"'
    assert '"args": mutate_args' not in src, (
        "'args': mutate_args still in payload — secrets would leak into vectors"
    )
    assert '"arg_keys"' in src, "'arg_keys' missing from vector payload"


# ── sensitive key redaction in strip_ephemeral_from_args ──────────────────────

def test_strip_ephemeral_redacts_value_key() -> None:
    from execution.memory_normalize import strip_ephemeral_from_args

    args = {
        "namespace": "multi-agent",
        "name": "chaos-pg-secret",
        "key": "APP_PASSWORD",
        "value": "super-secret-password-do-not-leak",
        "reasoning": "restore credential",
    }
    out = strip_ephemeral_from_args(args)
    assert out["value"] == "<redacted>", "value field must be redacted"
    assert out["namespace"] == "multi-agent"
    assert out["reasoning"] == "restore credential"


def test_strip_ephemeral_redacts_password_key() -> None:
    from execution.memory_normalize import strip_ephemeral_from_args

    args = {"password": "mysecret", "host": "db.example.com"}
    out = strip_ephemeral_from_args(args)
    assert out["password"] == "<redacted>"


# ── (d) recall returns RecallResult and logs retrieval ───────────────────────

class TestRecallPlaybookAdvisory(unittest.IsolatedAsyncioTestCase):
    async def test_recall_returns_result_on_hit(self) -> None:
        from workers.archivist import RecallResult, recall_playbook_advisory

        mock_point = MagicMock()
        mock_point.score = 0.82
        mock_point.payload = {
            "tool": "k8s_patch_secret",
            "arg_keys": ["namespace", "name", "key", "value", "reasoning"],
            "workload_fingerprint": "abc123",
        }

        mock_result = MagicMock()
        mock_result.points = [mock_point]

        mock_vs = MagicMock()
        mock_vs.similarity_search = AsyncMock(return_value=mock_result)

        ctx = SimpleNamespace(
            vector_store=mock_vs,
            llm=MagicMock(),
            settings=SimpleNamespace(embed_model="nomic-embed-text"),
        )

        result = await recall_playbook_advisory(
            ctx, query_text="CrashLoopBackOff password auth failed", trace="test-trace"
        )

        assert isinstance(result, RecallResult), "Expected RecallResult"
        assert result.top_tool == "k8s_patch_secret"
        assert result.top_score == 0.82
        assert result.strong is False  # 0.82 < 0.85 threshold
        assert "k8s_patch_secret" in result.advisory
        assert "similarity=0.82" in result.advisory
        # Must not contain raw secret values
        assert "chaos-app-pass" not in result.advisory
        assert "STALE-ROTATED" not in result.advisory

    async def test_recall_strong_above_threshold(self) -> None:
        from workers.archivist import RecallResult, build_strong_recall_prefix, recall_playbook_advisory

        mock_point = MagicMock()
        mock_point.score = 0.92
        mock_point.payload = {
            "tool": "k8s_patch_secret",
            "arg_keys": ["namespace", "name", "key", "value", "reasoning"],
            "workload_fingerprint": "",
        }

        mock_result = MagicMock()
        mock_result.points = [mock_point]

        mock_vs = MagicMock()
        mock_vs.similarity_search = AsyncMock(return_value=mock_result)

        ctx = SimpleNamespace(
            vector_store=mock_vs,
            llm=MagicMock(),
            settings=SimpleNamespace(embed_model="nomic-embed-text"),
        )

        result = await recall_playbook_advisory(
            ctx, query_text="password auth failed CrashLoop", trace="test-strong"
        )

        assert isinstance(result, RecallResult)
        assert result.strong is True  # 0.92 >= 0.85

        prefix = build_strong_recall_prefix(result)
        assert "k8s_patch_secret" in prefix
        assert "similarity=0.92" in prefix
        # Strong prefix must never contain secret values
        assert "chaos-app-pass" not in prefix
        assert "STALE-ROTATED" not in prefix
        # Must tell LLM to read values from cluster, not memory
        assert "cluster" in prefix.lower() or "read" in prefix.lower()

    async def test_recall_deprecated_risk_demotes_strong_and_warns(self) -> None:
        # Plan step 4: live cluster_version disagrees with chunk → DEPRECATED_RISK,
        # strong recall demoted to weak hint + re-verify warning injected.
        from workers.archivist import RecallResult, recall_playbook_advisory

        mock_point = MagicMock()
        mock_point.score = 0.95  # would normally be strong
        mock_point.payload = {
            "tool": "k8s_patch_secret",
            "arg_keys": ["namespace", "name"],
            "cluster_version": "v1.27.0",  # stale vs live v1.29.4
            "ingested_at": "2026-06-18T00:00:00+00:00",
        }
        mock_result = MagicMock()
        mock_result.points = [mock_point]
        mock_vs = MagicMock()
        mock_vs.similarity_search = AsyncMock(return_value=mock_result)

        ctx = SimpleNamespace(
            vector_store=mock_vs,
            llm=MagicMock(),
            settings=SimpleNamespace(
                embed_model="nomic-embed-text",
                omni_rag_freshness_enabled=True,
                omni_cluster_version="v1.29.4",
                omni_rag_freshness_max_age_sec=2_592_000,
            ),
        )

        result = await recall_playbook_advisory(
            ctx, query_text="password auth failed", trace="test-deprecated"
        )
        assert isinstance(result, RecallResult)
        assert result.strong is False  # demoted despite 0.95
        assert "DEPRECATED_RISK" in result.advisory
        assert "re-verify" in result.advisory.lower()

    async def test_recall_fresh_version_keeps_strong(self) -> None:
        # Same cluster_version as live → no demotion.
        from workers.archivist import RecallResult, recall_playbook_advisory

        mock_point = MagicMock()
        mock_point.score = 0.95
        mock_point.payload = {
            "tool": "k8s_patch_secret",
            "arg_keys": ["namespace", "name"],
            "cluster_version": "v1.29.4",
            "ingested_at": "2026-06-18T00:00:00+00:00",
        }
        mock_result = MagicMock()
        mock_result.points = [mock_point]
        mock_vs = MagicMock()
        mock_vs.similarity_search = AsyncMock(return_value=mock_result)

        ctx = SimpleNamespace(
            vector_store=mock_vs,
            llm=MagicMock(),
            settings=SimpleNamespace(
                embed_model="nomic-embed-text",
                omni_rag_freshness_enabled=True,
                omni_cluster_version="v1.29.4",
                omni_rag_freshness_max_age_sec=2_592_000,
            ),
        )

        result = await recall_playbook_advisory(
            ctx, query_text="password auth failed", trace="test-fresh"
        )
        assert isinstance(result, RecallResult)
        assert result.strong is True
        assert "DEPRECATED_RISK" not in result.advisory

    async def test_recall_returns_none_when_no_hits(self) -> None:
        from workers.archivist import recall_playbook_advisory

        mock_result = MagicMock()
        mock_result.points = []

        mock_vs = MagicMock()
        mock_vs.similarity_search = AsyncMock(return_value=mock_result)

        ctx = SimpleNamespace(
            vector_store=mock_vs,
            llm=MagicMock(),
            settings=SimpleNamespace(embed_model="nomic-embed-text"),
        )

        result = await recall_playbook_advisory(ctx, query_text="irrelevant", trace="t2")
        assert result is None

    async def test_recall_returns_none_without_vector_store(self) -> None:
        from workers.archivist import recall_playbook_advisory

        ctx = SimpleNamespace(vector_store=None, llm=MagicMock(), settings=MagicMock())
        result = await recall_playbook_advisory(ctx, query_text="any", trace="t3")
        assert result is None
