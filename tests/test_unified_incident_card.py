"""Unit tests for the unified Telegram incident card (one form, all lanes)."""

from workers.unified_incident_card import (
    AuditMeta,
    UnifiedCard,
    render_audit_footer,
    render_unified_card,
)


def test_audit_footer_always_has_trace():
    out = render_audit_footer("trace-abcd1234")
    assert "TRACE:" in out
    assert "#abcd1234" in out
    assert "Quyết định & Audit" in out


def test_audit_footer_renders_decision_mode_crat():
    audit = AuditMeta(
        mode="minimal",
        decision="SUGGEST",
        origin="llm",
        action="Chỉ đề xuất",
        crat_seq=1364,
        crat_signed=True,
        crat_event="ADVISORY_DISPATCHED",
    )
    out = render_audit_footer("t-xyz9999", audit)
    assert "minimal (xử lý lỗi cơ bản)" in out
    assert "chỉ đề xuất (cần người duyệt)" in out
    assert "Nguồn: llm" in out
    assert "CRAT: #1364" in out
    assert "đã ký ✓" in out


def test_audit_footer_plain_has_no_markdown_asterisks():
    audit = AuditMeta(mode="shadow", decision="HITL")
    out = render_audit_footer("t-1", audit, markdown=False)
    assert "*" not in out
    assert "`" not in out
    assert "TRACE:" in out


def test_unified_card_has_all_canonical_sections():
    card = UnifiedCard(
        lane="siem",
        verdict="CONFIRMED",
        title="SIEMDdos [critical] — ddos",
        trace_id="trace-deadbeef",
        what="DDoS từ IP ngoài",
        where="ns=prod · ip=1.2.3.4",
        why=("event A", "event B"),
        how_to=("kubectl get netpol",),
        forecast=(("1h", "critical", "leo thang"),),
        audit=AuditMeta(decision="HITL", origin="deterministic_siem"),
    )
    out = render_unified_card(card)
    assert "[SIEM]" in out
    assert "Chuyện gì đang xảy ra?" in out
    assert "Ở đâu? (Workload)" in out
    assert "Vì sao? (Bước kiểm chứng)" in out
    assert "Cách khắc phục?" in out
    assert "Dự báo tác động" in out
    assert "+1h: [CRITICAL]" in out
    assert "Quyết định & Audit" in out
    assert "#deadbeef" in out


def test_unified_card_header_badge_per_lane():
    for lane, badge in (("resource", "RESOURCE"), ("state", "STATE_FAIL"), ("app_log", "APP_LOG")):
        card = UnifiedCard(lane=lane, verdict="CRITICAL", title="x", trace_id="t")
        assert f"[{badge}]" in render_unified_card(card)
