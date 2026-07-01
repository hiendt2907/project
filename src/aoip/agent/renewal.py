"""Renewal coordinator — chạy renew định kỳ SONG SONG với một mutation dài.

Vì sao tồn tại: execution lease và Gateway delivery visibility đều có TTL độc lập; một
mutation chạy lâu hơn TTL cần renew ĐỊNH KỲ trong lúc mutation vẫn diễn ra, không phải
renew MỘT LẦN. ``run_with_renewal`` bọc một coroutine chính bằng một background task gọi
``renew_fn()`` mỗi ``interval_s``; nếu renew thất bại (ownership_lost), coroutine chính
KHÔNG bị huỷ giữa chừng (side effect có thể đã xảy ra — huỷ mù không an toàn hơn), nhưng
cờ ``lost`` được set để caller quyết định outcome sau khi coroutine chính xong (xem
``operations.run_guarded_recovery`` / ``delivery_loop.DeliveryLoop`` — họ map ``lost``
sang domain semantics riêng, KHÔNG phải module này).

Task renewal LUÔN được cancel + await sau khi coroutine chính xong (kể cả lỗi) — không
để lại orphan task, không nuốt CancelledError.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RenewalOutcome:
    """Kết quả kèm cờ mất ownership tại BẤT KỲ thời điểm nào trong lúc coroutine chạy."""

    __slots__ = ("result", "ownership_lost", "renew_error")

    def __init__(self, result: T, *, ownership_lost: bool, renew_error: str = "") -> None:
        self.result = result
        self.ownership_lost = ownership_lost
        self.renew_error = renew_error


async def run_with_renewal(
    coro: Awaitable[T], *, renew_fn: Callable[[], Awaitable[bool]], interval_s: float,
    label: str = "renewal",
) -> RenewalOutcome:
    """Chạy ``coro`` trong khi một task nền gọi ``renew_fn()`` mỗi ``interval_s``.

    ``renew_fn`` trả True (renew thành công) / False (ownership_lost — dependency vẫn
    chạy nhưng KHÔNG còn là owner hợp lệ, KHÔNG raise cho lỗi mạng thoáng qua vốn nên
    retry ở lần sau, không phải mất ownership). Một khi ``ownership_lost`` set True, task
    nền DỪNG renew tiếp (không có ý nghĩa renew nữa) nhưng ``coro`` vẫn được chờ xong.
    """
    lost = False
    error = ""
    stop = asyncio.Event()

    async def _loop() -> None:
        nonlocal lost, error
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval_s)
                    return  # stop() được gọi trước khi tới hạn renew tiếp theo
                except asyncio.TimeoutError:
                    pass
                if stop.is_set():
                    return
                try:
                    ok = await renew_fn()
                except Exception as exc:  # noqa: BLE001 — lỗi renew KHÔNG được crash mutation đang chạy
                    logger.info("%s.renew_error error=%s", label, exc)
                    continue
                if not ok:
                    lost = True
                    error = "ownership_lost"
                    logger.info("%s.ownership_lost", label)
                    return
        except asyncio.CancelledError:
            raise  # KHÔNG nuốt cancellation

    task = asyncio.create_task(_loop())
    try:
        result = await coro
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return RenewalOutcome(result, ownership_lost=lost, renew_error=error)
