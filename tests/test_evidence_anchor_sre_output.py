"""Evidence anchor + SRE output compaction."""

from __future__ import annotations

from pkg.reasoning.evidence_anchor import llm_contradicts_sdk_facts, summarize_facts_for_anchor
from pkg.reasoning.sre_output import compact_sre_diagnosis, strip_sre_fluff


def test_llm_contradicts_false_alarm_vs_high_cpu() -> None:
    ev = "FALSE_ALARM: x STALE_METRIC: y status: PASSED"
    llm = "The pod shows high CPU usage and needs scaling."
    assert llm_contradicts_sdk_facts(llm, ev) is True


def test_llm_agrees_false_alarm() -> None:
    ev = "FALSE_ALARM: x"
    llm = "FALSE_ALARM: metrics mismatch; STALE_METRIC: prom lag."
    assert llm_contradicts_sdk_facts(llm, ev) is False


def test_completed_vs_crash_language() -> None:
    ev = "status: Completed phase: Succeeded"
    llm = "Pod is in CrashLoopBackOff."
    assert llm_contradicts_sdk_facts(llm, ev) is True


def test_completed_vs_high_cpu_language() -> None:
    ev = "status: Completed phase: Succeeded probe: k8s_clinical_pod_metrics"
    llm = "High CPU usage — scale up replicas."
    assert llm_contradicts_sdk_facts(llm, ev) is True


def test_uncertain_llm_does_not_contradict_without_symptom() -> None:
    ev = "status: Completed phase: Succeeded"
    llm = "Possibly a transient issue; unclear from the evidence."
    assert llm_contradicts_sdk_facts(llm, ev) is False


def test_uncertain_but_elevated_cpu_still_contradicts() -> None:
    ev = "status: Completed phase: Succeeded"
    llm = "Possibly elevated CPU spike — worth checking HPA."
    assert llm_contradicts_sdk_facts(llm, ev) is True


def test_summarize_facts_for_anchor() -> None:
    docs = [{"result": "PASSED", "extracted_fact": '{"cpu":"1m"}'}]
    s = summarize_facts_for_anchor(docs)
    assert "PASSED" in s


def test_compact_truncates_words() -> None:
    long = "word " * 200
    c = compact_sre_diagnosis(long, max_words=20)
    assert len(c.split()) <= 20


def test_strip_fluff_removes_greeting_line() -> None:
    raw = "Hello! Here's the summary.\nSymptom: pod pending."
    s = strip_sre_fluff(raw)
    assert "Symptom" in s
    assert "Hello" not in s
