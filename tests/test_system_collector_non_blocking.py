"""P1 #5 — collect_system_metrics không được đóng băng event loop 1s/chu kỳ.

Bối cảnh (audit 2026-08-10, docs/audit/BACKEND_AUDIT_PLAN_2026-08-10.md #5):
`psutil.cpu_percent(interval=1)` nội bộ làm `time.sleep(1)` đồng bộ. Gọi thẳng
trong `async def` (không qua `run_in_executor`) đóng băng CẢ event loop của
remote_agent 1s mỗi chu kỳ thu thập — kể cả vòng poll command-channel
(`agent.py`, `_CMD_POLL_INTERVAL=5`) đang chạy chung loop, làm chậm phản hồi
lệnh chẩn đoán. Fix: đẩy `cpu_percent(interval=1)` sang thread pool qua
`run_in_executor`, event loop vẫn phục vụ được task khác trong lúc chờ.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest


def _fake_psutil(cpu_delay_s: float = 0.0) -> MagicMock:
    fake = MagicMock()

    def _slow_cpu_percent(interval=None):
        if cpu_delay_s:
            time.sleep(cpu_delay_s)  # mô phỏng đúng psutil thật: sleep ĐỒNG BỘ
        return 20.0

    fake.cpu_percent.side_effect = _slow_cpu_percent
    fake_mem = MagicMock(percent=40.0, used=512 * 1024 * 1024, total=2048 * 1024 * 1024)
    fake.virtual_memory.return_value = fake_mem
    fake_disk = MagicMock(percent=30.0, used=10 * 1024**3, total=50 * 1024**3)
    fake.disk_usage.return_value = fake_disk
    fake.getloadavg.return_value = (0.5, 0.4, 0.3)
    return fake


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_during_cpu_percent_collection():
    """Một coroutine khác chạy song song phải được phục vụ trong lúc cpu_percent
    'ngủ' — nếu bị gọi trực tiếp (không qua executor) event loop sẽ đứng hình và
    heartbeat dưới đây sẽ KHÔNG tăng đủ số lần trong thời gian chờ."""
    from remote_agent.collectors import system as sys_mod

    heartbeats = 0

    async def _heartbeat_loop():
        nonlocal heartbeats
        while True:
            await asyncio.sleep(0.05)
            heartbeats += 1

    fake_psutil = _fake_psutil(cpu_delay_s=0.3)
    with patch.dict("sys.modules", {"psutil": fake_psutil}):
        hb_task = asyncio.create_task(_heartbeat_loop())
        await sys_mod.collect_system_metrics("test-host")
        hb_task.cancel()

    # 0.3s collection / 0.05s heartbeat tick ≈ 6 nhịp khả dĩ; đòi hỏi ≥3 để chắc
    # chắn loop không bị treo cứng (bao dung lịch CI chậm), thay vì đòi khớp tuyệt đối.
    assert heartbeats >= 3, (
        f"event loop bị block trong lúc thu thập CPU — chỉ có {heartbeats} heartbeat"
    )


@pytest.mark.asyncio
async def test_cpu_percent_still_called_with_interval_one():
    from remote_agent.collectors import system as sys_mod

    fake_psutil = _fake_psutil()
    with patch.dict("sys.modules", {"psutil": fake_psutil}):
        result = await sys_mod.collect_system_metrics("test-host")

    fake_psutil.cpu_percent.assert_called_once_with(1)
    assert result["extracted_fact"]["cpu_percent"] == 20.0
