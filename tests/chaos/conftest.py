"""
Chaos test suite configuration — markers, fixtures, and reporting.

Marker phân loại:
  @pytest.mark.inverted_logic
      Test dùng điều kiện ngược lại (ví dụ: inject z_cpu < ngưỡng thật)
      để kiểm tra business logic. Lab không có real load nên phải dùng
      synthetic condition. CẦN LƯU Ý: test pass ≠ system sẽ detect
      real load thật trong production.

  @pytest.mark.real_condition
      Test dùng điều kiện thật (ví dụ: Redis lỗi thật, Kafka lỗi thật)
      thông qua mock/FakeRedis inject thực sự fail.

  @pytest.mark.business_logic_only
      Test chỉ kiểm tra business logic path, không kiểm tra
      infrastructure path (Prometheus scrape, kube-state-metrics, ...).
"""

from __future__ import annotations

import time
from typing import Generator

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "inverted_logic: test dùng synthetic/inverted condition để kiểm tra business logic. "
        "KHÔNG kiểm tra real metric collection hay real infra detection.",
    )
    config.addinivalue_line(
        "markers",
        "real_condition: test tạo real failure condition (via mock/FakeRedis/FakeKafka).",
    )
    config.addinivalue_line(
        "markers",
        "business_logic_only: test chỉ kiểm tra logic xử lý, không phải infra path.",
    )
    config.addinivalue_line(
        "markers",
        "slo: test kiểm tra SLO budget (MTTD/MTTR/SLO_SEC).",
    )


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Add inverted_logic marker automatically based on test naming conventions."""
    for item in items:
        name = item.name.lower()
        if any(kw in name for kw in ("cold_start", "warmup", "boundary", "threshold")):
            item.add_marker(pytest.mark.inverted_logic)
        if any(kw in name for kw in ("fail_closed", "suppress", "maint", "stale")):
            item.add_marker(pytest.mark.business_logic_only)


@pytest.fixture
def chaos_report(request: pytest.FixtureRequest) -> Generator[dict, None, None]:
    """Fixture to collect per-test acceptance criteria results."""
    report: dict = {
        "test": request.node.name,
        "markers": [m.name for m in request.node.iter_markers()],
        "checks": [],
        "start": time.time(),
        "inverted_logic": False,
    }
    # Detect inverted logic marker
    if any(m.name == "inverted_logic" for m in request.node.iter_markers()):
        report["inverted_logic"] = True

    yield report

    elapsed = round(time.time() - report["start"], 3)
    passes = [c for c in report["checks"] if c["ok"]]
    fails = [c for c in report["checks"] if not c["ok"]]

    lines = [f"\n  {'─'*60}"]
    lines.append(f"  Chaos Test: {report['test']}")
    if report["inverted_logic"]:
        lines.append(
            "  ⚠ INVERTED LOGIC: synthetic condition — tests business logic, NOT real detection"
        )
    lines.append(f"  Elapsed   : {elapsed}s")
    for check in report["checks"]:
        icon = "✓" if check["ok"] else "✗"
        lines.append(f"  {icon} {check['label']}")
    verdict = "PASS" if not fails else f"FAIL ({len(fails)} checks)"
    lines.append(f"  Verdict   : {verdict}")
    lines.append(f"  {'─'*60}")

    if fails:
        pytest.fail("\n".join(lines))
    else:
        print("\n".join(lines))
