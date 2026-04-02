"""Unknown tool feedback for agentic ReAct loop."""

from __future__ import annotations

from workers.tools import TOOL_REGISTRY, format_unknown_tool_feedback_en


def test_format_unknown_tool_feedback_lists_registry_en() -> None:
    msg = format_unknown_tool_feedback_en("fake_tool_xyz", unattended=False)
    assert "fake_tool_xyz" in msg
    assert "TOOL_REGISTRY" in msg
    assert "25 words" in msg
    assert "English" in msg
    for name in ("echo", "inspect_pod_deep", "escalate_to_human"):
        assert name in msg or f"`{name}`" in msg


def test_unattended_excludes_reply_from_catalog() -> None:
    msg = format_unknown_tool_feedback_en("bad_tool", unattended=True)
    if "reply" in TOOL_REGISTRY:
        assert "`reply`" not in msg


def test_unattended_feedback_mentions_escalate() -> None:
    msg = format_unknown_tool_feedback_en("nope", unattended=True)
    assert "escalate_to_human" in msg
