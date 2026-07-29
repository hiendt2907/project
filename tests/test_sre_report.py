"""G4/S4 — báo cáo SRE định kỳ, đọc được bởi người không rành kỹ thuật.

Thuần tuý: nhận dữ liệu đã thu thập, trả markdown. Không I/O, không secret.
"""

from __future__ import annotations

from pkg.reasoning.capacity_advisor import analyze_capacity
from pkg.reasoning.sre_report import build_sre_report


def _capacity(n=2):
    return [
        analyze_capacity(samples=[5.0] * 60, metric="cpu", host=f"h{i}", tenant_id="acme")
        for i in range(n)
    ]


def test_report_has_tenant_and_period_header():
    md = build_sre_report(
        tenant_id="acme", period_days=7, rates={"total": 0},
        graduations=[], capacity=[], topology_facts=0,
    )

    assert "acme" in md
    assert "7" in md


def test_report_states_no_data_plainly_instead_of_faking_health():
    md = build_sre_report(
        tenant_id="acme", period_days=7, rates={"total": 0},
        graduations=[], capacity=[], topology_facts=0,
    )

    assert "chưa có" in md.lower()


def test_report_includes_acceptance_when_present():
    md = build_sre_report(
        tenant_id="acme", period_days=14,
        rates={"total": 50, "accepted": 45, "rejected": 5, "false_positive": 0,
               "acceptance_rate": 0.9, "fp_rate": 0.0},
        graduations=[], capacity=[], topology_facts=10,
    )

    assert "90%" in md


def test_report_lists_graduated_playbooks():
    md = build_sre_report(
        tenant_id="acme", period_days=7, rates={"total": 0},
        graduations=[{"playbook_id": "abc123", "state": "GRADUATED",
                      "success_count": 5, "fail_count": 0, "domain": "advisory"}],
        capacity=[], topology_facts=0,
    )

    assert "abc123" in md
    assert "GRADUATED" in md


def test_report_surfaces_urgent_capacity_first():
    urgent = analyze_capacity(samples=[95.0] * 60, metric="disk", host="db-1",
                              tenant_id="acme", threshold=90.0)
    calm = analyze_capacity(samples=[5.0] * 60, metric="cpu", host="app-1",
                            tenant_id="acme")

    md = build_sre_report(
        tenant_id="acme", period_days=7, rates={"total": 0},
        graduations=[], capacity=[calm, urgent], topology_facts=0,
    )

    assert md.index("db-1") < md.index("app-1")


def test_report_never_leaks_secret_values():
    """INV_DATA_RESIDENCY — báo cáo chỉ ghi nhận sự tồn tại, không chép giá trị."""
    md = build_sre_report(
        tenant_id="acme", period_days=7, rates={"total": 0}, graduations=[],
        capacity=[], topology_facts=3,
        notes=["Phát hiện secret chưa xoay vòng ở dịch vụ billing"],
    )

    assert "billing" in md
    assert "password" not in md.lower()


def test_report_is_markdown_with_sections():
    md = build_sre_report(
        tenant_id="acme", period_days=7, rates={"total": 0}, graduations=[],
        capacity=_capacity(), topology_facts=5,
    )

    assert md.startswith("#")
    assert md.count("\n## ") >= 4
