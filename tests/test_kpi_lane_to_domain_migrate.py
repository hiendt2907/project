"""Di trú khoá KPI lane→domain (`scripts/kpi_lane_to_domain_migrate.py`).

Chạy trên `fakeredis` thật (không mock ZSET — quy ước dự án): tính chất cần chứng
minh là tính chất của DỮ LIỆU sau khi gộp, mà mock ZSET thì không có dữ liệu nào.

Hai tính chất quan trọng nhất, đều là chống-bùa-số:
  1. dry-run KHÔNG ghi gì;
  2. `SYS_HARD_FAIL` gộp vào `unknown`, KHÔNG phân bổ sang database/storage/service.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import fakeredis.aioredis
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "kpi_lane_to_domain_migrate",
    Path(__file__).resolve().parents[1] / "scripts/kpi_lane_to_domain_migrate.py",
)
assert _SPEC and _SPEC.loader
mig = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mig)


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def _run(redis, *, apply: bool):
    """Gọi ``migrate`` với client sẵn có thay vì URL — không cần Redis thật."""
    import contextlib

    original = mig.aioredis.from_url
    redis.aclose = _noop  # type: ignore[attr-defined]
    mig.aioredis.from_url = lambda *a, **k: redis  # type: ignore[assignment]
    try:
        return await mig.migrate("redis://fake/0", apply=apply)
    finally:
        with contextlib.suppress(Exception):
            mig.aioredis.from_url = original  # type: ignore[assignment]


async def _noop():
    return None


# ── Khuôn khoá ───────────────────────────────────────────────────────────────


def test_parse_key_tach_dung_tenant_va_lane():
    assert mig.parse_key("omni:kpi:detected:acme:SYS_RESOURCE") == (
        "omni:kpi:detected", "acme", "SYS_RESOURCE",
    )
    assert mig.parse_key("omni:kpi:resolved:acme:os_host") == (
        "omni:kpi:resolved", "acme", "os_host",
    )


def test_parse_key_tu_choi_khuon_la_thay_vi_cat_bua():
    """Cắt sai một khoá lạ rồi ghi đè là mất dữ liệu không tính lại được."""
    assert mig.parse_key("omni:kpi:detected:acme") is None
    assert mig.parse_key("omni:kpi:detected:acme:a:b") is None
    assert mig.parse_key("omni:kpi:z:acme:accepted") is None


def test_target_domain_chi_nhan_lane_truc_A():
    assert mig.target_domain("SYS_RESOURCE") == "os_host"
    assert mig.target_domain("app_http") == "application"
    assert mig.target_domain("SIEM_SECURITY") == "security"
    # Đã là domain canonical → None, nghĩa là không đụng tới.
    assert mig.target_domain("os_host") is None
    assert mig.target_domain("khong-biet-la-gi") is None


def test_sys_hard_fail_va_onboarding_ve_unknown_khong_doan():
    """Chống 'sửa cho đẹp': hai lane này gánh nhiều domain, đoán là bùa số."""
    assert mig.target_domain("SYS_HARD_FAIL") == "unknown"
    assert mig.target_domain("ONBOARDING_DISCOVERY") == "unknown"


# ── Hành vi di trú ───────────────────────────────────────────────────────────


async def test_dry_run_khong_ghi_gi(redis):
    await redis.zadd("omni:kpi:detected:acme:SYS_RESOURCE", {"t1": 100.0})
    moved = await _run(redis, apply=False)

    assert moved == 1
    assert await redis.exists("omni:kpi:detected:acme:SYS_RESOURCE") == 1
    assert await redis.exists("omni:kpi:detected:acme:os_host") == 0


async def test_apply_gop_va_xoa_khoa_cu(redis):
    """Để lại khoá cũ là `get_summary()` quét `detected:*` đếm hai lần một sự cố."""
    await redis.zadd("omni:kpi:detected:acme:SYS_RESOURCE", {"t1": 100.0, "t2": 200.0})
    await redis.zadd("omni:kpi:resolved:acme:APP_HTTP", {"t3": 300.0})

    moved = await _run(redis, apply=True)

    assert moved == 2
    assert await redis.exists("omni:kpi:detected:acme:SYS_RESOURCE") == 0
    assert await redis.zcard("omni:kpi:detected:acme:os_host") == 2
    assert await redis.zcard("omni:kpi:resolved:acme:application") == 1
    # Score là timestamp — phải giữ nguyên, cửa sổ 24h dựa vào nó.
    assert await redis.zscore("omni:kpi:detected:acme:os_host", "t2") == 200.0


async def test_gop_vao_dich_da_co_du_lieu_khong_mat_ban_ghi(redis):
    """Đường ghi hiện tại đã dùng khoá domain ⇒ đích thường đã có dữ liệu."""
    await redis.zadd("omni:kpi:detected:acme:os_host", {"moi": 500.0})
    await redis.zadd("omni:kpi:detected:acme:SYS_RESOURCE", {"cu": 100.0})

    await _run(redis, apply=True)

    assert await redis.zcard("omni:kpi:detected:acme:os_host") == 2


async def test_hai_lane_lossy_gop_chung_ro_khong_phan_bo_doan(redis, capsys):
    """SYS_HARD_FAIL + ONBOARDING_DISCOVERY cùng về `unknown`, và báo cáo nói rõ."""
    await redis.zadd("omni:kpi:detected:acme:SYS_HARD_FAIL", {"a": 1.0})
    await redis.zadd("omni:kpi:detected:acme:ONBOARDING_DISCOVERY", {"b": 2.0})

    await _run(redis, apply=True)
    out = capsys.readouterr().out

    assert await redis.zcard("omni:kpi:detected:acme:unknown") == 2
    for d in ("database", "storage", "service"):
        assert await redis.exists(f"omni:kpi:detected:acme:{d}") == 0
    assert "GỘP VÀO 'unknown'" in out
    assert "Không phân bổ sang database/storage/service" in out


async def test_chay_lai_khong_nhan_ban(redis):
    """Idempotent: lần hai không còn khoá lane nào, số liệu không đổi."""
    await redis.zadd("omni:kpi:detected:acme:SYS_RESOURCE", {"t1": 100.0})
    await _run(redis, apply=True)
    again = await _run(redis, apply=True)

    assert again == 0
    assert await redis.zcard("omni:kpi:detected:acme:os_host") == 1


async def test_khoa_outcome_khong_bi_dung_toi(redis):
    """`omni:kpi:z:{tenant}:{outcome}` không nhúng lane — không 'dọn cho đối xứng'."""
    await redis.zadd("omni:kpi:z:acme:accepted", {"t1": 1.0})
    await _run(redis, apply=True)
    assert await redis.zcard("omni:kpi:z:acme:accepted") == 1
