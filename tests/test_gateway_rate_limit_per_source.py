"""P1 #4 — rate limit /webhook/prometheus phải theo TỪNG NGUỒN GỌI, không dùng chung 1 bucket.

Bối cảnh (audit 2026-08-10, docs/audit/BACKEND_AUDIT_PLAN_2026-08-10.md #4): trước đây
`_rate_tokens` là 1 integer toàn cục — 1 nguồn ồn ào (đặc biệt nguy hiểm khi kết hợp với
#1, endpoint từng mở khi thiếu HMAC secret) chiếm hết budget của MỌI nguồn khác. Nay mỗi
client IP có bucket riêng (`_rate_tokens: OrderedDict[str, int]`), bounded LRU để không
phình bộ nhớ nếu nhiều IP lạ gọi vào.
"""

from __future__ import annotations

from gateway import api as gw


def _reset_bucket() -> None:
    gw._rate_tokens.clear()


def test_two_sources_each_get_their_own_full_budget() -> None:
    _reset_bucket()
    key_a, key_b = "10.0.0.1", "10.0.0.2"

    # Nguồn A tiêu hết budget của chính nó.
    for _ in range(gw.RATE_LIMIT_TPS):
        assert gw._take_rate_limit_token(key_a) is True
    assert gw._take_rate_limit_token(key_a) is False, "A phải bị chặn sau khi tiêu hết budget"

    # Nguồn B KHÔNG bị ảnh hưởng — đây chính là bug cũ (1 bucket dùng chung).
    assert gw._take_rate_limit_token(key_b) is True, (
        "nguồn B phải còn budget riêng, không bị A chiếm hết"
    )


def test_bucket_is_bounded_lru_evicts_oldest_source() -> None:
    _reset_bucket()
    cap = gw._MAX_RATE_LIMIT_KEYS

    for i in range(cap):
        gw._take_rate_limit_token(f"ip-{i}")
    assert len(gw._rate_tokens) == cap

    # Thêm 1 nguồn mới vượt cap — nguồn cũ nhất (ip-0) phải bị đuổi, không phình vô hạn.
    gw._take_rate_limit_token("ip-new")
    assert len(gw._rate_tokens) == cap
    assert "ip-0" not in gw._rate_tokens
    assert "ip-new" in gw._rate_tokens


async def test_refill_resets_every_tracked_key_and_survives_exception(monkeypatch, caplog) -> None:
    import asyncio
    import logging

    _reset_bucket()
    gw._rate_tokens["ip-x"] = 0
    gw._rate_tokens["ip-y"] = 3

    task = asyncio.create_task(gw._refill_tokens())
    await asyncio.sleep(1.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert gw._rate_tokens["ip-x"] == gw.RATE_LIMIT_TPS
    assert gw._rate_tokens["ip-y"] == gw.RATE_LIMIT_TPS
