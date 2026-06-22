"""Coverage: sanitize, alert_identity, reasoning.schema."""

from __future__ import annotations

import json

import pytest

from pkg.reasoning import alert_identity, sanitize
from pkg.reasoning import schema as evidence_schema


@pytest.mark.parametrize(
    "alert_hint,probe,want_prefix",
    [
        ("HighCPU on deployment", "redis_ping", "alert suggests"),
        ("memory pressure OOM", "redis_stream_len_inbound", "alert suggests"),
        ("", "redis_ping", None),
        ("cpu high", "", None),
        ("normal", "prom_pod_xyz", None),
        ("crash", "k8s_clinical_pod_status", None),
    ],
)
def test_evidence_relevance_warning(alert_hint: str, probe: str, want_prefix: str | None) -> None:
    w = sanitize.evidence_relevance_warning(alert_hint, probe)
    if want_prefix:
        assert w is not None and want_prefix in w
    else:
        assert w is None


def test_compact_canonical_snippet_and_format_sanitized() -> None:
    cq = json.dumps({"labels": {"alertname": "Test", "x": "y"}}, ensure_ascii=False)
    ev = {
        "alert_rule": "r1",
        "alert_hint": "hint " * 200,
        "canonical_query_snippet": cq,
        "evidence_source": "K8s_SDK",
        "clinical_priority_note": "note",
        "result": "ok",
        "probe": "p1",
        "symptom_group": "sg",
        "layer": "L3",
        "extracted_fact": {"cpu": 1},
        "raw": "rawbit",
        "ts": "1",
    }
    t = sanitize.format_sanitized_analyst_user_text(ev)
    assert "[ALERT_CONTEXT]" in t and "real-time K8s API" in t and "metrics_or_facts" in t

    flat = sanitize.format_sanitized_analyst_user_text(
        {
            "alert_rule": "",
            "canonical_query_snippet": "x" * 500,
            "evidence_source": "Prometheus",
            "result": "",
            "probe": "",
        }
    )
    assert "historical metrics" in flat


def test_format_batch_sanitized() -> None:
    out = sanitize.format_batch_sanitized_analyst_user_text(
        [{"probe": "a", "alert_rule": "r"}, {"probe": "b", "alert_rule": "r2"}]
    )
    assert "BATCH_DIAGNOSTIC" in out and "Probe block 1" in out


@pytest.mark.parametrize(
    "batch,want_empty",
    [
        ([], True),
        (
            [
                {
                    "probe": "k8s_clinical_pod_status",
                    "extracted_fact": {
                        "phase": "Pending",
                        "waiting_reasons": ["Unschedulable"],
                        "container_signals": ["sig"],
                    },
                }
            ],
            False,
        ),
    ],
)
def test_filter_evidence_for_rag(batch: list, want_empty: bool) -> None:
    r = sanitize.filter_evidence_for_rag(batch, max_tokens=256)
    if want_empty:
        assert "empty batch" in r
    else:
        assert "[RAG_QUERY]" in r and "container_reason" in r


def test_filter_evidence_events_and_http_strip() -> None:
    batch = [
        {
            "probe": "k8s_clinical_pod_events",
            "raw": "HTTP/1.1 400 Bad Request\ncontent-type: application/json\nevent CrashLoop",
        },
        {"symptom_group": "sg", "layer": "L1", "probe": "other"},
    ]
    r = sanitize.filter_evidence_for_rag(batch, max_tokens=512)
    assert "critical_events" in r


def test_parse_extracted_fact_json_string_in_format() -> None:
    ev = {
        "alert_rule": "r",
        "extracted_fact": '{"items": [], "reason": "Waiting"}',
        "canonical_query_snippet": '{"labels": {"alertname": "X"}}',
        "evidence_source": "other",
        "result": "x",
        "probe": "p",
    }
    t = sanitize.format_sanitized_analyst_user_text(ev)
    assert "EVIDENCE" in t


def test_alert_name_from_batch_alert_rule_fallback() -> None:
    batch = [{"canonical_query_snippet": "notjson", "alert_rule": "MyRule"}]
    r = sanitize.filter_evidence_for_rag(batch, max_tokens=300)
    assert "MyRule" in r or "alert_name" in r


@pytest.mark.parametrize(
    "raw,expect",
    [
        (None, None),
        ("false", False),
        ("TRUE", True),
        ("maybe", None),
    ],
)
def test_parse_omni_verify_required(raw: str | None, expect: bool | None) -> None:
    assert alert_identity.parse_omni_verify_required(raw) is expect


def test_signal_dna_and_labels_helpers() -> None:
    labels = {
        "alertname": "A",
        "namespace": "ns1",
        "deployment_name": "d1",
        "omni.io/symptom-group": "cpu",
        "omni.io/layer": "L4",
        "omni_verify_required": "1",
    }
    dna = alert_identity.parse_signal_dna_from_labels(labels)
    bits = dna.identity_bits_for_error_hint()
    assert "alertname=A" in bits and dna.omni_verify_required is True

    empty = alert_identity.labels_dict_from_canonical_query_snippet("not json")
    assert empty == {}

    assert alert_identity.infer_root_cause_id("x/y", "") == "x_y"
    assert alert_identity.infer_root_cause_id("", "Alert/Name") == "alert_name"

    pl = alert_identity.resolution_labels_payload(
        root_cause_id="id",
        root_cause_desc="desc",
        resolution_tool="t",
        verify_method="m",
    )
    assert pl["omni.io/root-cause-id"] == "id"


@pytest.mark.parametrize(
    "obj,expect_kind",
    [
        ("bad", "invalid"),
        ({"trace_id": "t1", "extracted_fact": {"a": 1}}, "t1"),
    ],
)
def test_coerce_evidence_dict(obj: object, expect_kind: str) -> None:
    out = evidence_schema.coerce_evidence_dict(obj)
    if expect_kind == "invalid":
        assert out.get("kind") == "invalid"
    else:
        assert out["trace_id"] == expect_kind
        assert "extracted_fact" in out


def test_coerce_evidence_dict_trace_fallback() -> None:
    out = evidence_schema.coerce_evidence_dict({"extracted_fact": [1, 2]})
    assert out["trace_id"] == "evidence-unknown"
    assert "[" in out["extracted_fact"]
