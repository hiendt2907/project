"""Collector mạng — cổng lắng nghe vừa đóng (domain=network).

Vì sao có collector này: `network` từng là domain DUY NHẤT có lệnh chẩn đoán trong
catalogue (18 lệnh) nhưng KHÔNG detector nào phát cảnh báo — `port_scan` là
`signal_type=DISCOVERY` nên chỉ đi vào knowledge pipeline. Đo được trên VM thật
2026-07-30: nginx bị dừng, cổng 80 đóng, Omni không sinh sự cố nào.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from remote_agent.collectors import network as net


@pytest.fixture(autouse=True)
def _clean_memory():
    """Trí nhớ chu kỳ trước là biến MODULE — không reset sẽ lây nhiễm giữa test."""
    net._reset_listener_memory()
    yield
    net._reset_listener_memory()


_SS_TWO_PORTS = """Netid State  Recv-Q Send-Q Local Address:Port Peer Address:Port
tcp   LISTEN 0      511          0.0.0.0:80         0.0.0.0:*
tcp   LISTEN 0      128          0.0.0.0:22         0.0.0.0:*
"""

_SS_ONE_PORT = """Netid State  Recv-Q Send-Q Local Address:Port Peer Address:Port
tcp   LISTEN 0      128          0.0.0.0:22         0.0.0.0:*
"""


class TestParseListeners:
    def test_ipv4_and_ipv6_same_port_collapse(self) -> None:
        """Một dịch vụ bind cả hai họ địa chỉ KHÔNG phải hai dịch vụ.

        Không gộp thì khi nó tắt ta báo hai lần cho cùng một sự cố.
        """
        ss = (
            "tcp LISTEN 0 511 0.0.0.0:80 0.0.0.0:*\n"
            "tcp LISTEN 0 511    [::]:80    [::]:*\n"
        )
        assert net.parse_listeners(ss) == {("tcp", 80)}

    def test_ipv6_address_port_taken_from_last_colon(self) -> None:
        """`[::]:80` có nhiều dấu hai chấm — cắt ở dấu ĐẦU sẽ ra rác."""
        assert net.parse_listeners("tcp LISTEN 0 511 [::1]:8080 [::]:*\n") == {("tcp", 8080)}

    def test_ephemeral_ports_ignored(self) -> None:
        """Cổng ephemeral là của client, không phải dịch vụ — theo dõi sẽ nhiễu liên tục."""
        assert net.parse_listeners("tcp LISTEN 0 128 0.0.0.0:45678 0.0.0.0:*\n") == set()

    def test_header_line_ignored(self) -> None:
        assert net.parse_listeners(
            "Netid State Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
        ) == set()

    def test_udp_kept_separately_from_tcp(self) -> None:
        ss = (
            "tcp LISTEN 0 511 0.0.0.0:53 0.0.0.0:*\n"
            "udp UNCONN 0 0   0.0.0.0:53 0.0.0.0:*\n"
        )
        assert net.parse_listeners(ss) == {("tcp", 53), ("udp", 53)}

    def test_garbage_line_does_not_crash(self) -> None:
        assert net.parse_listeners("rac rac rac rac khong:phai-so\n") == set()


class TestCollectListeningPorts:
    @pytest.mark.asyncio
    async def test_first_cycle_reports_nothing(self) -> None:
        """Chưa có trí nhớ ⇒ không kết luận. Không thì mọi cổng đều trông như 'vừa đóng'."""
        with patch.object(net, "_run", AsyncMock(return_value=(_SS_TWO_PORTS, "", 0))):
            env = await net.collect_listening_ports("host1")
        assert env is not None
        assert env["result"] == "PASSED"
        assert env["extracted_fact"]["first_cycle"] is True
        assert env["extracted_fact"]["lost_listeners"] == []

    @pytest.mark.asyncio
    async def test_detects_closed_port(self) -> None:
        with patch.object(
            net, "_run",
            AsyncMock(side_effect=[(_SS_TWO_PORTS, "", 0), (_SS_ONE_PORT, "", 0)]),
        ):
            await net.collect_listening_ports("host1")
            env = await net.collect_listening_ports("host1")

        assert env is not None
        assert env["result"] == "FAILED"
        assert env["alert_rule"] == "NetworkListenerLost"
        assert env["domain"] == "network"
        assert env["lane"] == "SYS_HARD_FAIL"
        assert env["extracted_fact"]["lost_listeners"] == ["tcp/80"]

    @pytest.mark.asyncio
    async def test_result_failed_reaches_extracted_fact(self) -> None:
        """`assess_domain_severity` Priority 1 đọc `extracted_fact.result`, không phải
        `result` top-level. Thiếu là urgency không lên critical và Stage 4 không chạy."""
        with patch.object(
            net, "_run",
            AsyncMock(side_effect=[(_SS_TWO_PORTS, "", 0), (_SS_ONE_PORT, "", 0)]),
        ):
            await net.collect_listening_ports("host1")
            env = await net.collect_listening_ports("host1")

        assert env["extracted_fact"]["result"] == "FAILED"

        from pkg.reasoning.domain_signals import assess_domain_severity

        severity = assess_domain_severity(
            env["domain"], env["alert_hint"], env["raw"], env["extracted_fact"]
        )
        assert severity in ("critical", "high"), f"severity={severity} — Stage 4 se khong chay"

    @pytest.mark.asyncio
    async def test_new_port_is_not_a_failure(self) -> None:
        """Mở thêm cổng là thay đổi, KHÔNG phải sự cố — không được báo động."""
        with patch.object(
            net, "_run",
            AsyncMock(side_effect=[(_SS_ONE_PORT, "", 0), (_SS_TWO_PORTS, "", 0)]),
        ):
            await net.collect_listening_ports("host1")
            env = await net.collect_listening_ports("host1")

        assert env["result"] == "PASSED"
        assert env["extracted_fact"]["new_listeners"] == ["tcp/80"]
        assert env["extracted_fact"]["lost_listeners"] == []

    @pytest.mark.asyncio
    async def test_stable_ports_stay_passed(self) -> None:
        with patch.object(
            net, "_run",
            AsyncMock(side_effect=[(_SS_TWO_PORTS, "", 0), (_SS_TWO_PORTS, "", 0)]),
        ):
            await net.collect_listening_ports("host1")
            env = await net.collect_listening_ports("host1")
        assert env["result"] == "PASSED"
        assert env["extracted_fact"]["lost_count"] == 0

    @pytest.mark.asyncio
    async def test_ss_unavailable_returns_none_not_false_alarm(self) -> None:
        """`ss` lỗi KHÔNG được biến thành 'mọi cổng đã đóng' — đó là báo động giả toàn bộ."""
        with patch.object(net, "_run", AsyncMock(return_value=("", "not found", 1))):
            assert await net.collect_listening_ports("host1") is None

    @pytest.mark.asyncio
    async def test_ss_failure_does_not_wipe_memory(self) -> None:
        """Một lần đọc lỗi rồi phục hồi không được tạo báo động giả ở chu kỳ sau."""
        with patch.object(
            net, "_run",
            AsyncMock(side_effect=[
                (_SS_TWO_PORTS, "", 0),   # chu kỳ 1: ghi nhớ 2 cổng
                ("", "boom", 1),          # chu kỳ 2: ss lỗi
                (_SS_TWO_PORTS, "", 0),   # chu kỳ 3: vẫn đủ 2 cổng
            ]),
        ):
            await net.collect_listening_ports("host1")
            assert await net.collect_listening_ports("host1") is None
            env = await net.collect_listening_ports("host1")

        assert env["result"] == "PASSED", "doc loi mot lan da lam mat tri nho"
        assert env["extracted_fact"]["lost_listeners"] == []

    @pytest.mark.asyncio
    async def test_collector_goes_through_exec_guard(self) -> None:
        """Collector KHÔNG có đường chạy lệnh riêng — cùng validator với command channel."""
        with patch.object(net.exec_guard, "check", return_value="lenh khong trong catalogue"):
            assert await net.collect_listening_ports("host1") is None
