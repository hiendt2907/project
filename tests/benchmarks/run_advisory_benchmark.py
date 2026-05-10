"""Advisory quality benchmark — runs golden cases through run_advisory_analyst().

Usage:
    python tests/benchmarks/run_advisory_benchmark.py [--model qwen2.5:7b] [--output results/]

Exit code 0 = all cases pass threshold (>= 70/100).
Exit code 1 = one or more cases below threshold.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

GOLDEN_DIR = Path(__file__).parent / "advisory_golden"
RESULTS_DIR = Path(__file__).parent / "results"
PASS_THRESHOLD = 70.0


def _score_result(expected: dict, advisory: dict | None) -> tuple[float, dict]:
    """Score one advisory against expected. Returns (total_score, breakdown)."""
    if advisory is None:
        return 0.0, {"error": "no advisory returned"}

    breakdown: dict[str, float] = {}

    # Verdict match (30 pts)
    expected_verdict = expected.get("verdict", "")
    got_verdict = advisory.get("verdict", "")
    breakdown["verdict"] = 30.0 if got_verdict == expected_verdict else 0.0

    # Root cause keywords (20 pts)
    root_cause = (advisory.get("root_cause") or "").lower()
    keywords = expected.get("root_cause_contains", [])
    if keywords:
        matched = sum(1 for kw in keywords if kw.lower() in root_cause)
        breakdown["root_cause_keywords"] = round(20.0 * matched / len(keywords), 1)
    else:
        breakdown["root_cause_keywords"] = 20.0

    # No hallucination — should_not_contain (20 pts)
    should_not = expected.get("should_not_contain", [])
    full_text = json.dumps(advisory).lower()
    if should_not:
        hallucinated = sum(1 for term in should_not if term.lower() in full_text)
        breakdown["no_hallucination"] = round(20.0 * (1 - hallucinated / len(should_not)), 1)
    else:
        breakdown["no_hallucination"] = 20.0

    # Remediation completeness (15 pts)
    remediation = advisory.get("proposed_remediation") or []
    approval_required = expected.get("remediation_approval_required", False)
    has_remediation = len(remediation) > 0
    approval_correct = not approval_required or any(
        s.get("approval_required") for s in remediation if isinstance(s, dict)
    )
    pts = 0.0
    if has_remediation:
        pts += 10.0
    if approval_correct:
        pts += 5.0
    breakdown["remediation"] = pts

    # Verification steps count (15 pts)
    min_steps = expected.get("min_verification_steps", 1)
    got_steps = len(advisory.get("verification_steps") or [])
    breakdown["verification_steps"] = 15.0 if got_steps >= min_steps else round(15.0 * got_steps / max(min_steps, 1), 1)

    total = sum(breakdown.values())
    return round(total, 1), breakdown


async def _run_case(case: dict, model: str, llm_url: str) -> dict:
    """Run one golden case and return scored result."""
    from workers.advisory_analyst_handler import run_advisory_analyst
    from unittest.mock import AsyncMock, MagicMock

    # Minimal WorkerSettings stub — benchmark only needs llm_base_url
    ws = MagicMock()
    ws.ollama_base_url = llm_url
    ws.ollama_model = model
    ws.llm_semaphore_limit = 1
    ws.rag_top_k = 3
    ws.rag_min_score = 0.0

    # Minimal fake redis that returns empty for RAG
    redis = AsyncMock()
    redis.hgetall = AsyncMock(return_value={})
    redis.zadd = AsyncMock()
    redis.rpush = AsyncMock()

    # Fake kafka capture
    kafka = MagicMock()
    kafka.send_dict = AsyncMock()

    ctx = SimpleNamespace(redis=redis, kafka=kafka, settings=ws)

    t0 = time.time()
    try:
        advisory = await run_advisory_analyst(
            ctx=ctx,
            evidence_text=case["evidence_text"],
            trace=f"benchmark-{case['id']}",
        )
        elapsed = time.time() - t0
        advisory_dict = advisory.model_dump() if advisory else None
    except Exception as e:
        elapsed = time.time() - t0
        advisory_dict = None
        return {
            "case_id": case["id"],
            "description": case["description"],
            "elapsed_s": round(elapsed, 2),
            "score": 0.0,
            "pass": False,
            "error": str(e),
            "breakdown": {},
        }

    score, breakdown = _score_result(case["expected"], advisory_dict)
    return {
        "case_id": case["id"],
        "description": case["description"],
        "elapsed_s": round(elapsed, 2),
        "score": score,
        "pass": score >= PASS_THRESHOLD,
        "verdict_got": (advisory_dict or {}).get("verdict"),
        "verdict_expected": case["expected"].get("verdict"),
        "breakdown": breakdown,
    }


async def run_benchmark(model: str, llm_url: str) -> dict:
    cases = sorted(GOLDEN_DIR.glob("case_*.json"))
    if not cases:
        print(f"No golden cases found in {GOLDEN_DIR}", file=sys.stderr)
        return {"results": [], "pass_rate": 0.0, "passed": 0, "total": 0}

    results = []
    for case_path in cases:
        case = json.loads(case_path.read_text())
        print(f"  [{case['id']}] {case['description'][:60]}...", end=" ", flush=True)
        result = await _run_case(case, model, llm_url)
        status = "PASS" if result["pass"] else "FAIL"
        print(f"{status} score={result['score']:.1f} ({result['elapsed_s']:.1f}s)")
        results.append(result)

    passed = sum(1 for r in results if r["pass"])
    total = len(results)
    pass_rate = round(passed / total, 4) if total else 0.0
    avg_score = round(sum(r["score"] for r in results) / total, 1) if total else 0.0

    return {
        "model": model,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pass_threshold": PASS_THRESHOLD,
        "passed": passed,
        "total": total,
        "pass_rate": pass_rate,
        "avg_score": avg_score,
        "results": results,
    }


def _publish_metrics(report: dict) -> None:
    try:
        import workers.metrics_exporter as me
        model = report.get("model", "unknown")
        for result in report.get("results", []):
            me.set_advisory_benchmark_score(result["case_id"], model, result["score"])
        me.set_advisory_benchmark_pass_rate(report.get("pass_rate", 0.0))
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Advisory quality benchmark")
    parser.add_argument("--model", default=os.getenv("OMNI_OLLAMA_MODEL", "qwen2.5:7b"))
    parser.add_argument("--llm-url", default=os.getenv("OMNI_OLLAMA_BASE_URL", "http://localhost:11434"))
    parser.add_argument("--output", default=str(RESULTS_DIR))
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\nAdvisory Benchmark — model={args.model} threshold={PASS_THRESHOLD}")
    print("-" * 70)

    report = asyncio.run(run_benchmark(args.model, args.llm_url))

    print("-" * 70)
    print(f"Result: {report['passed']}/{report['total']} passed "
          f"({report['pass_rate'] * 100:.1f}%) avg_score={report['avg_score']:.1f}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.output) / f"benchmark_{ts}.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"Report saved: {out_path}")

    _publish_metrics(report)
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
