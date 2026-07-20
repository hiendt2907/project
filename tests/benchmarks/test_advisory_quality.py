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
    assert isinstance(expected.get("expect_impact_chain", False), bool)


def test_multi_tier_golden_cases_expect_impact_chain() -> None:
    """Multi-tier golden cases must declare expect_impact_chain so the live benchmark
    exercises cross-tier causal reasoning. Guards against regressions in the dataset."""
    cases = {c["id"]: c for c in _load_cases()}
    for cid in ("case_021", "case_022", "case_023"):
        assert cid in cases, f"missing multi-tier golden case {cid}"
        assert cases[cid]["expected"].get("expect_impact_chain") is True


def test_fake_advisory_impact_chain_validates_against_schema() -> None:
    """The fake advisories for multi-tier cases must round-trip through the real
    AnalystAdvisory schema with a populated impact_chain (backward-compatible field)."""
    from pkg.reasoning.analyst_advisory_schema import AnalystAdvisory
    from workers.advisory_analyst_handler import _repair_advisory_dict

    # case_021 fingerprint
    fake = _make_advisory_for("postgres could not fsync wal errors")
    fake["trace_id"] = "test-021"
    repaired = _repair_advisory_dict(fake)
    advisory = AnalystAdvisory(**repaired)
    assert len(advisory.impact_chain) == 2
    assert advisory.impact_chain[0].evidence_lane == "state"
    assert advisory.impact_chain[1].evidence_lane == "app_log"
    # Every edge anchored to a canonical lane.
    assert all(link.evidence_lane in {"state", "resource", "app_log", "siem"} for link in advisory.impact_chain)


def test_advisory_backward_compatible_without_impact_chain() -> None:
    """Advisories that omit impact_chain (legacy/single-tier) still validate; field defaults to []."""
    from pkg.reasoning.analyst_advisory_schema import AnalystAdvisory
    from workers.advisory_analyst_handler import _repair_advisory_dict

    fake = _make_advisory_for("used_memory=7.8gb")  # case_002, no impact_chain
    fake["trace_id"] = "test-002"
    advisory = AnalystAdvisory(**_repair_advisory_dict(fake))
    assert advisory.impact_chain == []


# ---------------------------------------------------------------------------
# Per-case advisory templates (keyed by unique substring in the USER evidence_text)
# Fingerprints must be unique across all 20 golden cases and absent from system prompt.
# Scoring rubric: verdict(30) + keywords(20) + no-hallucination(20) + remediation(15) + steps(15)
# ---------------------------------------------------------------------------

_CASE_ADVISORIES: dict[str, dict] = {
    # case_001: nginx CrashLoopBackOff — ConfigMap missing, CRITICAL
    "configmap 'nginx": {"verdict": "CRITICAL", "root_cause": "nginx-config ConfigMap missing, causing nginx pod CrashLoopBackOff", "approval_required": True},
    # case_002: Redis OOM, URGENT
    "used_memory=7.8gb": {"verdict": "URGENT", "root_cause": "Redis memory exhausted at 7.8GB, OOM eviction active", "approval_required": False},
    # case_003: Kafka consumer lag, URGENT
    "1247 messages": {"verdict": "URGENT", "root_cause": "Kafka consumer group lag spike — analyst backlog growing rapidly", "approval_required": False},
    # case_004: 5xx / 502 surge, CRITICAL
    "5xx error rate 34%": {"verdict": "CRITICAL", "root_cause": "502 Bad Gateway surge from upstream pods, 5xx error rate spiking", "approval_required": True},
    # case_005: SIEM DDoS single IP, CRITICAL
    "8420 requests in 60s": {"verdict": "CRITICAL", "root_cause": "DDoS attack detected, high request rate from single IP", "approval_required": True},
    # case_006: CPU within normal range, NORMAL
    "z_cpu=1.2 (below": {"verdict": "NORMAL", "root_cause": "CPU usage within normal range, z_cpu=1.2 below 3-sigma threshold", "approval_required": False},
    # case_007: ImagePullBackOff rollout stuck, CRITICAL
    "imagepullbackoff": {"verdict": "CRITICAL", "root_cause": "Image pull failure (ImagePullBackOff), deployment rollout blocked", "approval_required": True},
    # case_008: auth failure surge (401/403), URGENT
    "auth_failure errors: 892": {"verdict": "URGENT", "root_cause": "Authentication failure surge: 401 and 403 auth errors spiking above baseline", "approval_required": True},
    # case_009: Ollama CrashLoopBackOff / CUDA OOM, CRITICAL
    "cuda out of": {"verdict": "CRITICAL", "root_cause": "Ollama LLM service crashed (CrashLoopBackOff), CUDA out-of-memory", "approval_required": False},
    # case_010: CRAT hash mismatch (block seq), CRITICAL
    "block seq=1847": {"verdict": "CRITICAL", "root_cause": "CRAT audit chain integrity failure, hash mismatch at block seq", "approval_required": True},
    # case_011: SIEM DDoS volumetric multiple IPs, CRITICAL
    "203.0.113.1, 203.0.113.2": {"verdict": "CRITICAL", "root_cause": "DDoS volumetric attack from multiple source IPs, critical severity", "approval_required": True},
    # case_012: SIEM malware lateral movement, CRITICAL
    "category=malware": {"verdict": "CRITICAL", "root_cause": "Malware detected with lateral movement and C2 beacon activity", "approval_required": True},
    # case_013: Pod OOMKilled repeated, URGENT
    "oomkilled 3 times in the last": {"verdict": "URGENT", "root_cause": "Pod repeatedly OOMKilled, memory limit exceeded (exit code 137)", "approval_required": False},
    # case_014: CPU spike from CronJob batch workload, NORMAL
    "data-pipeline-batch": {"verdict": "NORMAL", "root_cause": "CPU spike from scheduled CronJob batch workload, normal pattern", "approval_required": False},
    # case_015: Multi-lane concurrent — CPU + 503, CRITICAL
    "z_cpu=4.8": {"verdict": "CRITICAL", "root_cause": "Multi-lane incident: CPU resource anomaly concurrent with 503 HTTP errors", "approval_required": True},
    # case_016: Data exfiltration, CRITICAL
    "category=data_exfil": {"verdict": "CRITICAL", "root_cause": "Data exfiltration detected, sensitive data egress exceeding baseline", "approval_required": True},
    # case_017: CRAT integrity violation (audit chain), CRITICAL
    "cryptographic regulatory audit trail": {"verdict": "CRITICAL", "root_cause": "CRAT audit chain integrity violation, hash sequence broken", "approval_required": True},
    # case_018: LLM degraded (not crashed), INVESTIGATE
    "omni_llm_up=0 sustained for 12": {"verdict": "INVESTIGATE", "root_cause": "LLM service degraded, Ollama endpoint health check failing", "approval_required": False},
    # case_019: Rate limiting self-resolved, NORMAL
    "429 rate started": {"verdict": "NORMAL", "root_cause": "Rate limiting triggered (429 responses) and self-resolved naturally", "approval_required": False},
    # case_020: Distributed auth failure, URGENT
    "auth failure surge detected": {"verdict": "URGENT", "root_cause": "Distributed auth failure surge: 401/403 error spike from multiple sources", "approval_required": True},
    # case_021: multi-tier disk -> DB WAL -> API 500, CRITICAL
    "could not fsync wal": {
        "verdict": "CRITICAL",
        "root_cause": "Host /var/lib disk at 98% blocks Postgres WAL fsync, API write paths return HTTP 500",
        "approval_required": True,
        "impact_chain": [
            {"cause": "node disk /var/lib 98% full", "mechanism": "Postgres cannot fsync WAL", "effect": "DB write commits block", "evidence_lane": "state", "confidence": "high"},
            {"cause": "DB write commits blocked", "mechanism": "api-gateway handlers time out on commit", "effect": "API returns HTTP 500", "evidence_lane": "app_log", "confidence": "high"},
        ],
    },
    # case_022: multi-tier CPU saturation -> latency -> 504, URGENT
    "z_cpu=4.6 sustained": {
        "verdict": "URGENT",
        "root_cause": "Node CPU saturation starves checkout threads, request latency exceeds gateway timeout causing HTTP 504",
        "approval_required": False,
        "impact_chain": [
            {"cause": "node CPU saturated z_cpu=4.6", "mechanism": "checkout worker threads starved of CPU", "effect": "request latency rises to 9s", "evidence_lane": "resource", "confidence": "high"},
            {"cause": "latency exceeds gateway timeout", "mechanism": "load balancer aborts slow upstream", "effect": "client receives HTTP 504 timeout", "evidence_lane": "app_log", "confidence": "high"},
        ],
    },
    # case_023: multi-tier mem-leak -> OOMKilled -> 503, CRITICAL
    "+120mb/h over 6h": {
        "verdict": "CRITICAL",
        "root_cause": "Reporting service memory leak drives OOMKilled CrashLoopBackOff, endpoint returns 503 during restarts",
        "approval_required": False,
        "impact_chain": [
            {"cause": "reporting heap leak z_mem=5.1", "mechanism": "RSS crosses the 1Gi kubelet limit", "effect": "pod OOMKilled (exit 137)", "evidence_lane": "resource", "confidence": "high"},
            {"cause": "pod OOMKilled repeatedly", "mechanism": "CrashLoopBackOff removes the ready replica", "effect": "endpoint returns HTTP 503", "evidence_lane": "app_log", "confidence": "high"},
        ],
    },
}


def _make_advisory_for(evidence_lower: str) -> dict:
    """Return a tailored advisory that scores >= 70/100 against the matching golden case."""
    for fingerprint, tmpl in _CASE_ADVISORIES.items():
        if fingerprint in evidence_lower:
            advisory = {
                "verdict": tmpl["verdict"],
                # "unknown" — stub không biết workload thật; bịa một tên không có trong
                # evidence sẽ bị advisory grounding gate trung hoà (đúng thiết kế).
                "affected_workload": "unknown",
                "root_cause": tmpl["root_cause"],
                "verification_steps": [
                    {"command": "kubectl get pods -n multi-agent", "rationale": "check pod status"},
                    {"command": "kubectl describe pod -n multi-agent", "rationale": "inspect events"},
                    {"command": "kubectl logs -n multi-agent", "rationale": "read application logs"},
                ],
                "proposed_remediation": [
                    {
                        "action": "Apply targeted remediation",
                        "approval_required": tmpl["approval_required"],
                        "risk": "low",
                    }
                ],
                "forecast": None,
            }
            if tmpl.get("impact_chain"):
                advisory["impact_chain"] = tmpl["impact_chain"]
            return advisory
    # Should not reach here if all 20 golden cases have fingerprints above.
    return {
        "verdict": "INVESTIGATE",
        "affected_workload": "unknown",
        "root_cause": "Anomaly detected in workload. Investigation required.",
        "verification_steps": [
            {"command": "kubectl get pods -n multi-agent", "rationale": "check pod status"},
            {"command": "kubectl describe pod -n multi-agent", "rationale": "inspect events"},
        ],
        "proposed_remediation": [
            {"action": "Investigate manually", "approval_required": True, "risk": "unknown"}
        ],
        "forecast": None,
    }


class _FakeLLMClient:
    """Deterministic LLM stub — reads evidence_text from the USER message only and
    returns a scored-to-pass advisory JSON. No network calls; no Ollama required."""

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        **kwargs,
    ) -> dict:
        # Use only the user message to avoid false matches against system prompt content.
        user_content = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        advisory = _make_advisory_for(user_content.lower())
        return {"message": {"role": "assistant", "content": json.dumps(advisory)}}

    async def embed(self, *, model: str, prompt: str, **kwargs) -> dict:
        return {"embedding": [0.0] * 768}

    async def chat_plain(self, *, model: str, messages: list[dict], **kwargs) -> str:
        resp = await self.chat(model=model, messages=messages)
        return resp["message"]["content"]

    async def chat_structured(self, *, model: str, messages: list[dict], **kwargs) -> str:
        return await self.chat_plain(model=model, messages=messages)


def test_benchmark_pass_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run benchmark with a fake LLM — validates scoring rubric without live Ollama.

    The fake LLM returns deterministic per-case advisories that score >= 70/100
    on each golden case. This confirms the benchmark harness and scoring logic
    are correct, without requiring a live Ollama endpoint.
    """
    import asyncio
    import tests.benchmarks.run_advisory_benchmark as _bench

    fake_llm = _FakeLLMClient()
    # build_llm_client is imported locally inside _run_case → patch the source module
    import llm.factory as _llm_factory
    monkeypatch.setattr(_llm_factory, "build_llm_client", lambda **kwargs: fake_llm)

    model = os.getenv("OMNI_OLLAMA_MODEL", "test-model")
    llm_url = os.getenv("OMNI_OLLAMA_BASE_URL", "http://fake-ollama:11434")
    report = asyncio.run(_bench.run_benchmark(model, llm_url))

    assert report["pass_rate"] >= 0.7, (
        f"Benchmark pass rate {report['pass_rate']:.1%} < 70% threshold. "
        f"Failed cases: {[r['case_id'] for r in report['results'] if not r['pass']]}"
    )
