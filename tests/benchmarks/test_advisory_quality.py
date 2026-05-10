"""pytest wrapper for advisory quality benchmark — informational, non-blocking."""

import json
import os
import sys
from pathlib import Path

import pytest

GOLDEN_DIR = Path(__file__).parent / "advisory_golden"
PASS_THRESHOLD = 70.0

pytestmark = pytest.mark.benchmark


def _load_cases() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(GOLDEN_DIR.glob("case_*.json"))]


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_golden_case_schema(case: dict) -> None:
    """Validate golden dataset JSON schema — fast, no LLM needed."""
    assert "id" in case
    assert "description" in case
    assert "lane" in case
    assert "evidence_text" in case
    expected = case.get("expected", {})
    assert "verdict" in expected
    assert expected["verdict"] in ("NORMAL", "INVESTIGATE", "URGENT", "CRITICAL")
    assert isinstance(expected.get("root_cause_contains", []), list)
    assert isinstance(expected.get("should_not_contain", []), list)
    assert isinstance(expected.get("min_verification_steps", 1), int)
    assert isinstance(expected.get("remediation_approval_required", False), bool)


@pytest.mark.skipif(
    not os.getenv("OMNI_OLLAMA_BASE_URL"),
    reason="OMNI_OLLAMA_BASE_URL not set — skipping live LLM benchmark",
)
def test_benchmark_pass_rate() -> None:
    """Run full benchmark against live LLM — requires OMNI_OLLAMA_BASE_URL."""
    import asyncio
    from tests.benchmarks.run_advisory_benchmark import run_benchmark

    model = os.getenv("OMNI_OLLAMA_MODEL", "qwen2.5:7b")
    llm_url = os.getenv("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")
    report = asyncio.run(run_benchmark(model, llm_url))
    assert report["pass_rate"] >= 0.7, (
        f"Benchmark pass rate {report['pass_rate']:.1%} < 70% threshold. "
        f"Failed cases: {[r['case_id'] for r in report['results'] if not r['pass']]}"
    )
