"""`aoip.service_knowledge` — tra cứu cổng kỳ vọng theo tên tiến trình.

Tách ra từ `tests/test_aoip_expectation_loop.py` khi xoá walking-skeleton
(2026-08-09): file cũ phụ thuộc `aoip.capabilities.inspect_host` đã gỡ, nhưng
`service_knowledge` vẫn SỐNG — `aoip.understanding` dùng nó, và `understanding`
được `aoip/agent/main.py` (agent chạy trên VM khách) gọi. Không giữ lại test này
thì module sống mất sạch lưới an toàn.
"""
from __future__ import annotations

from aoip.service_knowledge import expected_ports


def test_expected_ports_knowledge_lookup():
    assert set(expected_ports("nginx")) == {80, 443}
    assert set(expected_ports("mariadbd")) == {3306}  # chuẩn hoá về tên gốc
    assert expected_ports("totally-unknown-daemon") == ()
