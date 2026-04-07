"""Two-channel parser for RAG-miss SDK-only analyst output."""

from __future__ import annotations

from pkg.reasoning.two_channel_sdk import parse_two_channel_sdk_only


def test_parse_two_channel_valid() -> None:
    text = """
MACHINE_JSON: {"verdict":"DIAGNOSE","hypothesis":"cpu high","action":{"tool":"","args":{}}}
HUMAN_SUMMARY: Pod shows high load on nginx container.
"""
    o = parse_two_channel_sdk_only(text)
    assert o["machine"] is not None
    assert o["machine"]["verdict"] == "DIAGNOSE"
    assert "nginx" in o["human"]


def test_parse_fallback_no_markers() -> None:
    o = parse_two_channel_sdk_only("plain text only")
    assert o["machine"] is None
    assert o["human"] == "plain text only"
