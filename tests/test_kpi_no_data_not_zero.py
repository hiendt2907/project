"""Bất biến: "CHƯA CÓ DỮ LIỆU" không bao giờ được rơi xuống 0.0.

Bug thật đo được 2026-08-02 trên pod `omni-fullstack` đang chạy:

    omni_kpi_advisory_acceptance_rate 0.0
    omni_kpi_false_positive_rate 0.0

Chưa ai set hai gauge đó lần nào — `_handle_feedback` chỉ chạy khi có sự kiện trên
`omni-action-feedback`, mà chế độ shadow không sinh sự kiện nào. `prometheus_client`
khởi tạo Gauge bằng 0.0 và xuất ngay từ giây đầu, nên tầng metric xoá mất sự phân biệt
mà `read_outcome_rates` cố ý giữ (trả `None` khi chưa có mẫu).

Hai hậu quả NGƯỢC CHIỀU nhau, nên không thể vá bằng cách đổi giá trị mặc định:
  - acceptance 0.0 → alert `OmniAdvisoryAcceptanceLow` fire vĩnh viễn từ lúc khởi động;
    đo được ~340 incident meta_self/ngày, chiếm gần trọn phần pipeline còn hoạt động.
  - fp_rate 0.0    → trông như "không có dương tính giả nào", fail-open bằng mắt.

Lời giải: xuất thêm `omni_kpi_advisory_total` làm mẫu số, và MỌI alert rule bám vào
tỉ lệ phải kèm `and omni_kpi_advisory_total > 0`.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from workers.kpi_metrics import KPIStore, kpi_outcome_key

_PROM_RULES = Path(__file__).resolve().parents[1] / "k8s" / "monitor" / "prometheus.yaml"


class FakeRedis:
    """ZSET tối thiểu — dự án cấm AsyncMock cho ZSET (instinct 90%)."""

    def __init__(self) -> None:
        self.z: dict[str, dict[str, float]] = {}

    async def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)

    async def zremrangebyscore(self, key, lo, hi):
        return 0

    async def expire(self, key, ttl):
        return True

    async def zcount(self, key, lo, hi):
        lo_f = float("-inf") if str(lo).endswith("inf") else float(lo)
        return sum(1 for s in self.z.get(key, {}).values() if s >= lo_f)

    async def scan_iter(self, match, count=100):
        pattern = re.compile("^" + re.escape(match).replace(r"\*", ".*") + "$")
        for key in list(self.z):
            if pattern.match(key):
                yield key


async def test_summary_khong_co_mau_thi_ti_le_la_None_khong_phai_0():
    """Mẫu số 0 ⇒ tỉ lệ là None. Đây là điểm phân biệt "kém" với "chưa biết"."""
    store = KPIStore(FakeRedis())

    summary = await store.get_summary(tenant_id="default")

    assert summary["total_advisory"] == 0
    assert summary["acceptance_rate"] is None, "0.0 và None KHÔNG được lẫn lộn"
    assert summary["false_positive_rate"] is None


async def test_summary_luon_tra_mau_so_de_gauge_phan_biet_duoc():
    """Gauge không mang được None ⇒ mẫu số phải là một trường riêng, luôn có mặt."""
    store = KPIStore(FakeRedis())

    assert "total_advisory" in await store.get_summary(tenant_id="default")
    assert "total_advisory" in await store.get_summary()


async def test_summary_per_tenant_khong_tron_du_lieu_khach_khac():
    """Dữ liệu khách A không được lái chỉ số khách B (INV_NAMESPACE_ISOLATION).

    Trước bản vá, `get_summary` quét `omni:kpi:z:*:accepted` toàn cục và cộng lại.
    """
    redis = FakeRedis()
    store = KPIStore(redis)
    now = time.time()
    await redis.zadd(kpi_outcome_key("khach-a", "accepted"), {"t1": now})
    await redis.zadd(kpi_outcome_key("khach-b", "rejected"), {"t2": now})

    a = await store.get_summary(tenant_id="khach-a")
    b = await store.get_summary(tenant_id="khach-b")

    assert (a["accepted"], a["rejected"]) == (1, 0)
    assert (b["accepted"], b["rejected"]) == (0, 1)
    assert a["acceptance_rate"] == 1.0
    assert b["acceptance_rate"] == 0.0, "khách B bị chê thật — đây mới là 0.0 hợp lệ"


async def test_ghi_outcome_phai_qua_kpi_outcome_key():
    """Writer bắt buộc dùng hàm dựng key chung — không nối chuỗi thủ công.

    Bốn bản ghi test 83 ngày tuổi tìm thấy trong Redis lab (TTL=-1, sống sót qua cửa sổ
    24h) chứng minh có đường ghi KHÔNG đi qua `KPIStore`: nếu qua, `_zadd_and_expire`
    đã đặt TTL và `zremrangebyscore` đã cắt chúng từ lâu.
    """
    redis = FakeRedis()
    store = KPIStore(redis)

    await store.record_accepted("trace-1", tenant_id="khach-a")
    await store.record_rejected("trace-2", tenant_id="khach-a")
    await store.record_false_positive("trace-3", tenant_id="khach-a")

    for outcome in ("accepted", "rejected", "false_positive"):
        assert kpi_outcome_key("khach-a", outcome) in redis.z
    assert not [k for k in redis.z if not k.startswith("omni:kpi:z:khach-a:")]


@pytest.mark.parametrize(
    "alert_name",
    [
        "OmniAdvisoryAcceptanceRateLow",
        "OmniFalsePositiveRateHigh",
        "OmniAdvisoryAcceptanceLow",
        "OmniFalsePositiveRateTooHigh",
    ],
)
def test_moi_alert_ti_le_deu_co_cong_mau_so(alert_name: str):
    """Alert bám vào tỉ lệ mà thiếu `total > 0` là báo động giả vĩnh viễn."""
    text = _PROM_RULES.read_text(encoding="utf-8")
    idx = text.index(f"alert: {alert_name}")
    expr_line = text[idx:].split("expr:", 1)[1].split("\n", 1)[0]

    assert "omni_kpi_advisory_total > 0" in expr_line, (
        f"{alert_name} thiếu cổng mẫu số — sẽ fire ngay cả khi chưa có mẫu nào. "
        f"expr={expr_line.strip()}"
    )


def test_co_alert_bao_trang_thai_chua_biet():
    """"Chưa biết" phải THẤY ĐƯỢC, không phải sự im lặng."""
    text = _PROM_RULES.read_text(encoding="utf-8")

    assert "alert: OmniKpiNoSamples" in text
    assert "omni_kpi_advisory_total == 0" in text
