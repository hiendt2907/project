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
    from llm.factory import build_llm_client
    from unittest.mock import AsyncMock, MagicMock

    # Minimal WorkerSettings stub — benchmark only needs llm_base_url
    ws = MagicMock()
    ws.vllm_base_url = llm_url
    ws.ollama_base_url = llm_url
    ws.ollama_model = model
    ws.diag_evidence_llm_model = model
    ws.model_reasoning_engine = model
    ws.llm_num_ctx = int(os.getenv("BENCHMARK_NUM_CTX", "4096"))
    ws.llm_semaphore_limit = 1
    _timeout = float(os.getenv("BENCHMARK_LLM_TIMEOUT_SEC", "120"))
    ws.llm_chat_timeout_sec = _timeout
    ws.omni_advisory_num_predict = int(os.getenv("BENCHMARK_NUM_PREDICT", "512"))
    ws.rag_top_k = 3
    ws.rag_min_score = 0.0
    ws.max_reply_words = 800
    ws.reply_language = "en"

    # Minimal fake redis that returns empty for RAG
    redis = AsyncMock()
    redis.hgetall = AsyncMock(return_value={})
    redis.zadd = AsyncMock()
    redis.rpush = AsyncMock()
    redis.setex = AsyncMock()
    redis.get = AsyncMock(return_value=None)

    # Fake kafka capture
    kafka = MagicMock()
    kafka.send_dict = AsyncMock()

    # Real LLM client — HTTP timeout must exceed asyncio timeout to let wait_for fire cleanly
    llm = build_llm_client(base_url=llm_url, embed_url=llm_url, timeout_s=_timeout + 30)

    ctx = SimpleNamespace(redis=redis, kafka=kafka, settings=ws, llm=llm)

    # Bypass retry decorator (3 retries × timeout = too slow on local LLM).
    # Call the unwrapped function directly for benchmark.
    import workers.advisory_analyst_handler as _aah
    from unittest.mock import patch as _patch
    _orig_retry_fn = _aah._llm_chat_with_retry
    _unwrapped = getattr(_orig_retry_fn, "__wrapped__", None)
    if _unwrapped is not None:
        _aah._llm_chat_with_retry = _unwrapped

    t0 = time.time()
    try:
        # Patch write_audit_block — benchmark only tests LLM quality, not audit chain.
        # redis.pipeline() doesn't work with AsyncMock so CRAT would fail-closed otherwise.
        with _patch("workers.advisory_analyst_handler.write_audit_block", new=AsyncMock()):
            advisory = await run_advisory_analyst(
                ctx=ctx,
                payload={},
                evidence_text=case["evidence_text"],
                trace=f"benchmark-{case['id']}",
            )
        elapsed = time.time() - t0
        advisory_dict = advisory.model_dump(mode="json") if advisory else None
    except Exception as e:
        elapsed = time.time() - t0
        advisory_dict = None
        return {
            "case_id": case["id"],
            "description": case["description"],
            "elapsed_s": round(elapsed, 2),
            "score": 0.0,
            "pass": False,
            "error": type(e).__name__,
            "breakdown": {},
        }
    finally:
        # Restore retry wrapper
        if _unwrapped is not None:
            _aah._llm_chat_with_retry = _orig_retry_fn

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
    # Case không sinh ra advisory nào (JSON hỏng, bị cắt ở num_predict, hoặc LLM lỗi).
    # Đây là tín hiệu HẠ TẦNG/CẤU HÌNH, gần như nhị phân và ít nhiễu hơn điểm số rất
    # nhiều — nên nó là thứ đáng gate nhất. Đ74: chỉ số này nhảy 0 → 19/23 khi đổi
    # provider sang NIM, mà không ai thấy vì kết quả benchmark bị `|| true` nuốt.
    no_advisory = sum(1 for r in results if r.get("breakdown", {}).get("error") or r.get("error"))

    return {
        "model": model,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pass_threshold": PASS_THRESHOLD,
        "passed": passed,
        "total": total,
        "pass_rate": pass_rate,
        "avg_score": avg_score,
        "no_advisory_count": no_advisory,
        "num_predict": int(os.getenv("BENCHMARK_NUM_PREDICT", "512")),
        "num_ctx": int(os.getenv("BENCHMARK_NUM_CTX", "4096")),
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


BASELINE_PATH = Path(__file__).parent / "baseline.json"


def _check_gate(report: dict) -> tuple[bool, list[str]]:
    """So kết quả với baseline đã ghi. Trả (đạt, danh sách lý do trượt).

    CỐ Ý là gate CHỐNG THỤT LÙI, không phải gate chất lượng tuyệt đối. Lý do: đo
    ngày 2026-08-17 (Đ74) cho thấy pass-rate ở ngưỡng cố định 70 nhiễu cực mạnh —
    trung bình nhích 3.9 điểm thì pass-rate nhảy 21.7 điểm, vì nhiều case nằm sát
    vạch. Một gate tuyệt đối đặt trên chỉ số đó sẽ đỏ/xanh ngẫu nhiên rồi bị vô
    hiệu hoá, đúng số phận của `|| true` cũ. Chất lượng tuyệt đối là mục tiêu sản
    phẩm; CI chỉ nên chặn THỤT LÙI.
    """
    if not BASELINE_PATH.exists():
        return True, []
    base = json.loads(BASELINE_PATH.read_text())
    reasons: list[str] = []

    # (1) Tín hiệu hạ tầng — gần như nhị phân, ít nhiễu. Đây là thứ đã bắt hụt
    #     suốt hơn một tháng, nên nó chặn cứng.
    max_no_adv = int(base.get("max_no_advisory", 0))
    got_no_adv = int(report.get("no_advisory_count", 0))
    if got_no_adv > max_no_adv:
        reasons.append(
            f"{got_no_adv} case không sinh ra advisory (cho phép tối đa {max_no_adv}). "
            "Thường là JSON bị cắt ở num_predict hoặc LLM lỗi — xem log "
            "event=advisory_analyst_truncated / event=llm_response_truncated."
        )

    # (2) Điểm trung bình — ổn định hơn pass-rate nhiều, dùng biên dung sai vì
    #     biến động run-to-run có thật (quan sát được ±30 điểm trên từng case).
    tol = float(base.get("avg_score_tolerance", 5.0))
    floor = float(base["avg_score"]) - tol
    if float(report.get("avg_score", 0.0)) < floor:
        reasons.append(
            f"avg_score {report['avg_score']} < sàn {floor:.1f} "
            f"(baseline {base['avg_score']} − dung sai {tol})."
        )
    return not reasons, reasons


def main() -> int:
    parser = argparse.ArgumentParser(description="Advisory quality benchmark")
    parser.add_argument("--model", default=os.getenv("OMNI_OLLAMA_MODEL", "qwen2.5:7b"))
    parser.add_argument("--llm-url", default=os.getenv("OMNI_OLLAMA_BASE_URL", "http://localhost:11434"))
    parser.add_argument("--output", default=str(RESULTS_DIR))
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Thoát khác 0 khi thụt lùi so với tests/benchmarks/baseline.json.",
    )
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\nAdvisory Benchmark — model={args.model} threshold={PASS_THRESHOLD}")
    print("-" * 70)

    report = asyncio.run(run_benchmark(args.model, args.llm_url))

    print("-" * 70)
    print(f"Result: {report['passed']}/{report['total']} passed "
          f"({report['pass_rate'] * 100:.1f}%) avg_score={report['avg_score']:.1f} "
          f"no_advisory={report['no_advisory_count']} "
          f"num_predict={report['num_predict']} num_ctx={report['num_ctx']}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.output) / f"benchmark_{ts}.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"Report saved: {out_path}")

    _publish_metrics(report)

    if not args.gate:
        # Mã thoát cũ là `0 if passed == total else 1`, tức đòi 23/23 case hoàn
        # hảo — lần chạy tốt nhất từng đo (65.2%) vẫn thoát 1. Không dùng làm
        # gate được, nên nó bị bọc `|| true` rồi thành đồ trang trí. Chạy trần
        # giờ luôn thoát 0; muốn chặn thì dùng --gate.
        return 0

    ok, reasons = _check_gate(report)
    if ok:
        print("GATE: PASS (không thụt lùi so với baseline)")
        return 0
    print("GATE: FAIL")
    for r in reasons:
        print(f"  - {r}")
    print(f"  baseline: {BASELINE_PATH}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
