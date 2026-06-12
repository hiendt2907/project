"""Unit tests for the principle-based SIEM reasoning engine (siem_reasoning.py).

These assert the *principles* generalise to never-before-seen IPs/categories:
origin classification, source cardinality, pps parsing, and the WHY/VERIFY
conclusions derived from them — not canned per-category text.
"""

from __future__ import annotations

import pytest

from workers.siem_reasoning import (
    SiemEvidence,
    assess_cardinality,
    classify_origin,
    extract_siem_evidence,
    reason_blast_radius,
    reason_verify,
    reason_why,
)


# --- origin classification: generalises over arbitrary addresses --------------

@pytest.mark.parametrize(
    "ip,kind,internal",
    [
        ("10.0.0.42", "internal_rfc1918", True),
        ("172.16.4.2", "internal_rfc1918", True),
        ("192.168.1.1", "internal_rfc1918", True),
        ("127.0.0.1", "loopback", True),
        ("169.254.1.1", "link_local", True),
        ("8.8.8.8", "external_routable", False),
        ("1.1.1.1", "external_routable", False),
        ("", "unknown", False),
        ("not-an-ip", "unknown", False),
        ("n/a", "unknown", False),
    ],
)
def test_classify_origin(ip, kind, internal):
    oc = classify_origin(ip)
    assert oc.kind == kind
    assert oc.is_internal is internal


def _ev(**kw) -> SiemEvidence:
    base = dict(
        category="ddos", severity="critical", namespace="multi-agent", tenant="t1",
        incident_id="inc-1", description="", suggested_action="", source_ips=(), pps=None,
    )
    base.update(kw)
    return SiemEvidence(**base)


# --- cardinality is measured, not assumed -------------------------------------

def test_cardinality_single_multiple_unknown():
    assert assess_cardinality(_ev(source_ips=("8.8.8.8",))) == "single"
    assert assess_cardinality(_ev(source_ips=("8.8.8.8", "1.1.1.1"))) == "multiple"
    assert assess_cardinality(_ev(source_ips=())) == "unknown"


# --- WHY conclusions follow principles for unseen inputs ----------------------

def test_why_single_internal_blocks_edge_action():
    why = reason_why(_ev(source_ips=("10.0.0.42",))).lower()
    assert "một nguồn nội bộ duy nhất" in why
    assert "không phải tấn công phân tán" in why


def test_why_multiple_external_is_distributed():
    why = reason_why(_ev(source_ips=("8.8.8.8", "1.1.1.1", "9.9.9.9"))).lower()
    assert "phân tán" in why
    assert "single" not in why


def test_why_unknown_origin_is_not_dead_end():
    why = reason_why(_ev(category="brand_new_threat", source_ips=())).lower()
    assert "chưa xác nhận" in why
    assert "xác định (các) nguồn" in why
    # category it has never seen is still carried, not dropped
    assert "brand_new_threat" in why


def test_why_unseen_category_single_external():
    """A category absent from every table still produces a sound single-source claim."""
    why = reason_why(_ev(category="quantum_exploit", source_ips=("8.8.8.8",))).lower()
    assert "một nguồn bên ngoài duy nhất" in why
    assert "quantum_exploit" in why


# --- VERIFY always non-empty and principle-driven -----------------------------

def test_verify_always_has_classify_and_ingress():
    for ev in (_ev(source_ips=("8.8.8.8",)), _ev(source_ips=()), _ev(category="x", source_ips=("10.0.0.1",))):
        steps = reason_verify(ev)
        assert steps  # never empty
        joined = " ".join(steps).lower()
        assert "kết thúc bên trong cụm" in joined


# --- pps extraction from free text --------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("inbound pps 80k from 10.0.0.42", 80_000),
        ("80000 pps sustained", 80_000),
        ("rate 5k req/s", 5_000),
        ("2.5m packets/s", 2_500_000),
        ("no rate mentioned here", None),
    ],
)
def test_pps_parsing(text, expected):
    batch = [{
        "probe": "siem_incident_context",
        "raw": text,
        "extracted_fact": {"category": "ddos", "affected_ip": "8.8.8.8"},
    }]
    ev = extract_siem_evidence(batch, {})
    assert ev.pps == expected


def test_extract_collects_multiple_distinct_sources():
    batch = [
        {"probe": "siem_network_flow", "raw": "flows from 8.8.8.8 and 1.1.1.1", "extracted_fact": {}},
        {"probe": "siem_incident_context", "extracted_fact": {
            "category": "ddos", "affected_ip": "8.8.8.8", "source_ips": ["9.9.9.9"]}},
    ]
    ev = extract_siem_evidence(batch, {})
    assert set(ev.source_ips) == {"8.8.8.8", "1.1.1.1", "9.9.9.9"}
    assert assess_cardinality(ev) == "multiple"


def test_blast_radius_reflects_observed_pps():
    ev = _ev(source_ips=("8.8.8.8",), pps=80_000)
    line = reason_blast_radius(ev)
    assert "80,000 pps" in line
    assert "1 nguồn" in line
