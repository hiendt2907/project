"""Sự cố dịch vụ ĐANG DIỄN RA phải được báo mỗi chu kỳ, không chỉ một lần — Đ52.

Đo trên UAT thật 2026-08-11: `payment-api` ở trạng thái `enabled` + `inactive` (người vận
hành đã tuyên bố nó PHẢI chạy, và nó KHÔNG chạy — một outage sống), nhưng collector trả

    alert_hint = "[cust-app] systemd: all monitored services OK"
    result     = "PASSED"

Nguyên nhân: `_collect_units_that_stopped()` là **edge-triggered** — nó so tập active của
chu kỳ này với chu kỳ trước (`gone = prev - now_active`). Unit bắn đúng MỘT lần lúc chuyển
trạng thái; các chu kỳ sau nó không còn trong `prev` lẫn `now_active` nên `gone` rỗng vĩnh
viễn.

Ba hậu quả thật, không phải lý thuyết:
  1. Sự cố kéo dài chỉ được báo 1 lần. Lần đó LLM timeout (đo được: 77% lượt) ⇒ sự cố biến
     mất khỏi radar mãi mãi.
  2. Agent restart trong lúc dịch vụ đang chết ⇒ `prev is None` ⇒ `return [], []` ⇒ sự cố
     đó không bao giờ được phát hiện.
  3. Vòng tự khắc phục không thể chạy lại: không có evidence thì không có chẩn đoán.

Chính comment ở `collectors/services.py` đã tuyên bố hành vi ĐÚNG mà code không làm:
``# `enabled` + `inactive` cũng là FAILED: người vận hành đã tuyên bố unit phải chạy.``

Vì sao KHÔNG sửa bằng cách quét thẳng "enabled nhưng inactive": docstring gốc ghi rõ đã thử
và bị **15 unit nhiễu** (`systemd-pcrlock-*`/`systemd-timesyncd` có `ConditionResult=no`,
`dmesg` có `Type=idle`). Đánh đổi đó có lý. Nên bản vá giữ nguyên edge-trigger để PHÁT HIỆN
(đã lọc oneshot/condition sẵn), rồi NHỚ unit đã xác nhận dừng và tiếp tục báo tới khi nó chạy
lại — không kéo theo 15 unit nhiễu kia.
"""
from __future__ import annotations

import pytest

from remote_agent.collectors import services as svc


@pytest.fixture(autouse=True)
def _reset_state():
    svc._reset_service_state_memory()
    yield
    svc._reset_service_state_memory()


def _fake_run(active_units: list[str], props: dict[str, dict[str, str]] | None = None):
    """Giả lập `systemctl` ở mức lệnh — không mock hàm đang được kiểm."""
    props = props or {}

    async def _run(cmd, stdin=None, timeout=8.0):
        if cmd[:2] == ["systemctl", "list-units"] and "--state=active" in cmd:
            return ("\n".join(f"{u} loaded active running" for u in active_units), "", 0)
        if cmd[:2] == ["systemctl", "show"]:
            unit = cmd[-1]
            p = props.get(unit, {"Type": "simple", "RemainAfterExit": "no"})
            return ("\n".join(f"{k}={v}" for k, v in p.items()), "", 0)
        if cmd[:2] == ["systemctl", "is-active"]:
            unit = cmd[-1]
            return ("active" if unit in active_units else "inactive", "", 0)
        return ("", "", 0)

    return _run


async def test_su_co_dang_dien_ra_phai_duoc_bao_MOI_chu_ky(monkeypatch):
    """Đây là bất biến cốt lõi: outage kéo dài không được phép tàng hình sau 1 lần báo."""
    # Chu kỳ 1: payment-api đang chạy → chưa có gì để so
    monkeypatch.setattr(svc, "_run", _fake_run(["payment-api.service", "nginx.service"]))
    stopped, _ = await svc._collect_units_that_stopped()
    assert stopped == []

    # Chu kỳ 2: payment-api vừa dừng → phát hiện (edge)
    monkeypatch.setattr(svc, "_run", _fake_run(["nginx.service"]))
    stopped, _ = await svc._collect_units_that_stopped()
    assert stopped == ["payment-api"], "edge-trigger phải bắt được lúc chuyển trạng thái"

    # Chu kỳ 3,4,5: VẪN dừng → PHẢI tiếp tục báo. Trước bản vá, các chu kỳ này trả []
    # và outage biến mất khỏi radar.
    for cycle in (3, 4, 5):
        stopped, _ = await svc._collect_units_that_stopped()
        assert stopped == ["payment-api"], f"chu ky {cycle}: outage bi tang hinh"


async def test_dich_vu_chay_lai_thi_thoi_bao(monkeypatch):
    """Hết sự cố ⇒ phải im. Nếu không, cảnh báo sẽ kẹt vĩnh viễn — tệ hơn không có."""
    monkeypatch.setattr(svc, "_run", _fake_run(["payment-api.service"]))
    await svc._collect_units_that_stopped()

    monkeypatch.setattr(svc, "_run", _fake_run([]))
    assert (await svc._collect_units_that_stopped())[0] == ["payment-api"]

    # Dịch vụ được khôi phục
    monkeypatch.setattr(svc, "_run", _fake_run(["payment-api.service"]))
    assert (await svc._collect_units_that_stopped())[0] == []
    # và lần sau vẫn im
    assert (await svc._collect_units_that_stopped())[0] == []


async def test_oneshot_khong_bi_coi_la_su_co_keo_dai(monkeypatch):
    """`dmesg` (Type=idle/oneshot) thoát sau khi xong là ĐÚNG — không được nhớ nó.

    Đây chính là lớp nhiễu mà tác giả gốc cố tình tránh (15 unit trên VM thật). Bản vá
    không được làm sống lại nó.
    """
    props = {"dmesg.service": {"Type": "oneshot", "RemainAfterExit": "no"}}
    monkeypatch.setattr(svc, "_run", _fake_run(["dmesg.service"], props))
    await svc._collect_units_that_stopped()

    monkeypatch.setattr(svc, "_run", _fake_run([], props))
    stopped, skipped = await svc._collect_units_that_stopped()
    assert stopped == [] and skipped == ["dmesg"]

    # và các chu kỳ sau cũng không được báo
    for _ in range(3):
        assert (await svc._collect_units_that_stopped())[0] == []


async def test_nhieu_unit_cung_dung_deu_duoc_giu(monkeypatch):
    monkeypatch.setattr(svc, "_run", _fake_run(["a.service", "b.service", "c.service"]))
    await svc._collect_units_that_stopped()

    monkeypatch.setattr(svc, "_run", _fake_run(["c.service"]))
    assert (await svc._collect_units_that_stopped())[0] == ["a", "b"]
    assert (await svc._collect_units_that_stopped())[0] == ["a", "b"]

    # b sống lại, a vẫn chết
    monkeypatch.setattr(svc, "_run", _fake_run(["b.service", "c.service"]))
    assert (await svc._collect_units_that_stopped())[0] == ["a"]


async def test_reset_state_xoa_ca_tri_nho_outage(monkeypatch):
    """`_reset_service_state_memory()` phải xoá SẠCH, nếu không test khác nhiễm trạng thái."""
    monkeypatch.setattr(svc, "_run", _fake_run(["x.service"]))
    await svc._collect_units_that_stopped()
    monkeypatch.setattr(svc, "_run", _fake_run([]))
    assert (await svc._collect_units_that_stopped())[0] == ["x"]

    svc._reset_service_state_memory()
    assert (await svc._collect_units_that_stopped())[0] == []
