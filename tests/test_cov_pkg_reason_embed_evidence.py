"""Table-driven coverage for pkg.reason_codes, pkg.rag.embed_utils, pkg.reasoning.evidence_anchor."""

from __future__ import annotations

import pytest

from pkg.rag import embed_utils
from pkg.reasoning import evidence_anchor, reason_codes


@pytest.mark.parametrize(
    "code,expected",
    [
        (reason_codes.ERR_GOV_NS_OUT_OF_BOUNDS, "high"),
        (reason_codes.ERR_REA_NO_PHYSICAL_PROOF, "low"),
        ("", "info"),
        ("  ", "info"),
        ("unknown_code", "info"),
        (None, "info"),
    ],
)
def test_reason_severity_table(code: str | None, expected: str) -> None:
    assert reason_codes.reason_severity(code) == expected


@pytest.mark.parametrize(
    "text,max_tokens,expect_truncated",
    [
        ("", 512, False),
        ("   ", 512, False),
        ("hello", 512, False),
        ("x" * 300, 64, True),
    ],
)
def test_truncate_for_embedding(text: str, max_tokens: int, expect_truncated: bool) -> None:
    out = embed_utils.truncate_for_embedding(text, max_tokens=max_tokens)
    if expect_truncated:
        assert "[…truncated for embedding]" in out
    elif text.strip():
        assert out == text.strip()
    else:
        assert out == ""


def test_truncate_for_embedding_long_default_cap() -> None:
    cap = max(256, 512 * 4)
    body = "Z" * (cap + 50)
    out = embed_utils.truncate_for_embedding(body, max_tokens=512)
    assert len(out) <= cap + 40
    assert "truncated" in out.lower()


@pytest.mark.parametrize(
    "llm,ev,expect",
    [
        ("", "status: completed", False),
        ("maybe", "status: completed", False),
        ("maybe CrashLoopBackOff", "status: completed", True),
        ("Pod is in CrashLoopBackOff", "status: completed", True),
        ("High CPU spike observed", "status: completed", True),
        ("cpu spike observed", "batch shows STALE_METRIC and FALSE_ALARM", True),
        ("CPU high spike with FALSE_ALARM", "STALE_METRIC in evidence", False),
    ],
)
def test_llm_contradicts_sdk_facts(llm: str, ev: str, expect: bool) -> None:
    assert evidence_anchor.llm_contradicts_sdk_facts(llm, ev) is expect


@pytest.mark.parametrize(
    "docs,want_sub",
    [
        ([{"result": "ok", "extracted_fact": {"a": 1}}], "ok"),
        ([], " "),
    ],
)
def test_summarize_facts_for_anchor(docs: list, want_sub: str) -> None:
    s = evidence_anchor.summarize_facts_for_anchor(docs)
    assert want_sub in s or s.strip() == ""
