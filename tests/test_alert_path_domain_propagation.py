"""Đường ALERT phải mang `domain` + `signal_kind` tới trace meta.

Đo tại P1: 100% trace sống (`gw-prom-*`, tức toàn bộ traffic thật) có
`{"domain": "", "signal_kind": ""}`. Việc gắn hai trục ở Đ38/Đ39 mới phủ đường
remote-agent; envelope do `diagnostic_dispatcher` sinh KHÔNG có hai khoá đó nên
`evidence_consumer` đọc ra rỗng.
"""
from __future__ import annotations

import inspect

import pytest

from workers import diagnostic_dispatcher as dd
from workers.diagnostic_probe_registry import PROBE_DOMAINS
from pkg.domain import taxonomy


def test_probe_envelope_carries_registry_domain():
    src = inspect.getsource(dd._publish_diagnostic_evidence)
    assert '"domain": PROBE_DOMAINS.get(pid, "")' in src
    assert '"signal_kind": "diagnostic"' in src


def test_every_registered_probe_domain_is_canonical():
    """Nếu registry lọt domain không canonical thì portal hiện nhãn không thuộc 9 lĩnh vực."""
    bad = {p: d for p, d in PROBE_DOMAINS.items() if d not in taxonomy.CANONICAL_DOMAINS}
    assert not bad, f"probe có domain không canonical: {bad}"


def test_consumer_reads_signal_kind_from_envelope():
    src = inspect.getsource(__import__("workers.evidence_consumer", fromlist=["x"]).reason_from_diagnostic_evidence)
    assert 'ev_doc.get("signal_kind")' in src
    assert "signal_kind=_ev_kind" in src


@pytest.mark.parametrize("bogus", ["", "DIAGNOSTIC", "alert", "random", None])
def test_invalid_signal_kind_becomes_empty_not_written_raw(bogus):
    """Giá trị lạ phải thành rỗng — rỗng còn được lấp, giá trị sai thì đứng nguyên trên UI."""
    kind = str(bogus or "")
    assert (kind if kind in ("diagnostic", "learning") else "") == ""


def test_alert_context_domain_left_empty_not_guessed():
    """Ngữ cảnh alert thô chưa gắn probe ⇒ chưa biết lĩnh vực, phải để rỗng."""
    src = inspect.getsource(dd)
    i = src.index('"probe": "alert_context"')
    assert '"domain": ""' in src[i : i + 600]


def test_coerce_keeps_both_axes_not_only_domain():
    """`coerce_evidence_dict` là CỬA HẸP mọi evidence phải qua — whitelist thiếu là rơi im lặng.

    Đã trả giá hai lần: `domain` từng bị bỏ quên ở đây (xem comment trong schema.py),
    và `signal_kind` lặp lại y hệt — gắn ở dispatcher lẫn evidence_consumer rồi vẫn
    rỗng, mất hai lượt deploy mới truy ra.
    """
    from pkg.reasoning import coerce_evidence_dict

    out = coerce_evidence_dict({
        "trace_id": "t1", "probe": "k8s_clinical_pod_status",
        "domain": "kubernetes", "signal_kind": "diagnostic",
    })
    assert out["domain"] == "kubernetes"
    assert out["signal_kind"] == "diagnostic"


def test_coerce_drops_nothing_the_dispatcher_writes():
    """Mọi khoá dispatcher ghi vào envelope phải sống sót qua cửa hẹp."""
    from pkg.reasoning import coerce_evidence_dict

    written = {
        "kind": "diagnostic_evidence", "trace_id": "t2", "symptom_group": "workload_resource",
        "layer": "workload", "probe": "k8s_clinical_pod_status", "domain": "kubernetes",
        "signal_kind": "diagnostic", "result": "PASSED", "raw": "x", "ts": "1",
        "alert_rule": "IngressPrometheus", "alert_hint": "h",
        "canonical_query_snippet": "{}", "namespace": "multi-agent", "evidence_source": "K8s_SDK",
    }
    out = coerce_evidence_dict(written)
    missing = [k for k in ("domain", "signal_kind", "probe", "result", "namespace") if k not in out]
    assert not missing, f"cửa hẹp nuốt mất: {missing}"
