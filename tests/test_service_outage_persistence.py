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


# ── Outage ĐÃ TỒN TẠI trước khi agent khởi động ──────────────────────────────
#
# Edge-trigger về bản chất không thể thấy loại này: không có "chu kỳ trước" để so.
# Đo trên cust-app 2026-08-11: 18 unit `enabled`+`inactive`, nhưng áp CẢ HAI bộ lọc
# (`ConditionResult != no` và Type thuộc allowlist daemon) thì còn ĐÚNG 1 — payment-api,
# tức 0 false positive. Đây là phản chứng cho ghi chú gốc ("không có thuộc tính systemd
# nào phân biệt daemon với chạy-một-lần"): `Type` + `ConditionResult` cộng lại thì được.

def _fake_run_enabled_inactive(enabled_units, active_units, props):
    async def _run(cmd, stdin=None, timeout=8.0):
        if cmd[:2] == ["systemctl", "list-units"] and "--state=active" in cmd:
            return ("\n".join(f"{u} loaded active running" for u in active_units), "", 0)
        if cmd[:2] == ["systemctl", "list-unit-files"]:
            return ("\n".join(f"{u} enabled enabled" for u in enabled_units), "", 0)
        if cmd[:2] == ["systemctl", "is-active"]:
            return ("active" if cmd[-1] in active_units else "inactive", "", 0)
        if cmd[:2] == ["systemctl", "show"]:
            p = props.get(cmd[-1], {})
            return ("\n".join(f"{k}={v}" for k, v in p.items()), "", 0)
        return ("", "", 0)
    return _run


_REAL_VM_PROPS = {
    # daemon thật đang chết — PHẢI báo
    "payment-api.service": {"ConditionResult": "yes", "Type": "simple", "RemainAfterExit": "no"},
    # nhiễu thật quan sát được trên cust-app — KHÔNG được báo
    "dmesg.service": {"ConditionResult": "yes", "Type": "idle", "RemainAfterExit": "no"},
    "e2scrub_reap.service": {"ConditionResult": "yes", "Type": "oneshot", "RemainAfterExit": "no"},
    "systemd-timesyncd.service": {"ConditionResult": "no", "Type": "notify", "RemainAfterExit": "no"},
    "systemd-pcrlock-machine-id.service": {"ConditionResult": "no", "Type": "oneshot", "RemainAfterExit": "yes"},
    "ubuntu-advantage.service": {"ConditionResult": "no", "Type": "simple", "RemainAfterExit": "no"},
}


async def test_outage_co_truoc_khi_agent_khoi_dong_van_phai_thay(monkeypatch):
    """Agent restart lúc dịch vụ đã chết ⇒ vẫn phải phát hiện, không mù.

    Trước bản vá: `prev is None` ⇒ `return [], []` ⇒ sự cố không bao giờ được báo.
    Đây là ca thật đã gặp: payment-api chết lúc 12:53, agent restart 13:14, collector
    trả "all monitored services OK".
    """
    enabled = list(_REAL_VM_PROPS)
    monkeypatch.setattr(
        svc, "_run", _fake_run_enabled_inactive(enabled, [], _REAL_VM_PROPS)
    )
    stopped, _ = await svc._collect_units_that_stopped()
    assert stopped == ["payment-api"], f"phai thay dung payment-api, nhan duoc {stopped}"


async def test_khong_keo_theo_nhieu_tu_unit_he_thong(monkeypatch):
    """0 false positive trên đúng bộ dữ liệu VM thật — bất biến chống nhiễu."""
    enabled = list(_REAL_VM_PROPS)
    monkeypatch.setattr(
        svc, "_run", _fake_run_enabled_inactive(enabled, [], _REAL_VM_PROPS)
    )
    stopped, _ = await svc._collect_units_that_stopped()
    for noisy in ("dmesg", "e2scrub_reap", "systemd-timesyncd",
                  "systemd-pcrlock-machine-id", "ubuntu-advantage"):
        assert noisy not in stopped, f"{noisy} la nhieu, khong duoc bao"


async def test_dich_vu_khoe_thi_khong_bao_gi(monkeypatch):
    """Mọi thứ chạy bình thường ⇒ im lặng tuyệt đối."""
    enabled = list(_REAL_VM_PROPS)
    monkeypatch.setattr(
        svc, "_run",
        _fake_run_enabled_inactive(enabled, ["payment-api.service"], _REAL_VM_PROPS),
    )
    assert (await svc._collect_units_that_stopped())[0] == []
