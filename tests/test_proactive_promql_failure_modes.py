"""`_instant_scalar` phải phân biệt RULE HỎNG với CỤM ĐANG KHOẺ.

Cả hai đều trả ``None``, nhưng hậu quả vận hành ngược nhau:

- ``status != "success"`` — rule gõ sai PromQL. Nó sẽ **không bao giờ bắn nữa**, và
  trước khi có bản vá này thì không có một dòng log nào. Đây là kiểu hỏng tệ nhất:
  bảng đo vẫn xanh, engine chủ động vẫn "đang chạy", chỉ là mù một mắt vĩnh viễn.
- vector rỗng — bình thường. `kube_pod_container_status_waiting_reason` chỉ sinh
  series khi thật sự có container đang waiting; đo trên GCP 2026-08-09:
  `kube_pod_container_status_waiting` có 65 series còn `..._waiting_reason` có 0.
  Log ồn ào ở đây sẽ dạy operator bỏ qua cảnh báo, nên cố ý để mức debug.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from workers import proactive_observer


class _Ctx(SimpleNamespace):
    pass


async def _patched(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    async def _fake_get_json(ctx: Any, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(proactive_observer, "_prometheus_get_json", _fake_get_json)


async def test_broken_rule_is_loud(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    await _patched(monkeypatch, {"status": "error", "error": "parse error: unexpected }"})
    with caplog.at_level(logging.WARNING, logger=proactive_observer.logger.name):
        val = await proactive_observer._instant_scalar(_Ctx(), "sum(broken{")
    assert val is None
    assert "proactive_promql_rejected" in caplog.text
    assert "parse error" in caplog.text


async def test_healthy_cluster_is_quiet(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    await _patched(monkeypatch, {"status": "success", "data": {"resultType": "vector", "result": []}})
    with caplog.at_level(logging.WARNING, logger=proactive_observer.logger.name):
        val = await proactive_observer._instant_scalar(_Ctx(), "sum(kube_pod_container_status_waiting_reason)")
    assert val is None
    assert "proactive_promql_rejected" not in caplog.text


async def test_value_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    await _patched(
        monkeypatch,
        {"status": "success", "data": {"result": [{"metric": {}, "value": [1786279420.1, "2"]}]}},
    )
    assert await proactive_observer._instant_scalar(_Ctx(), "sum(x)") == 2.0


async def test_empty_vector_never_fires_a_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chốt hành vi: vector rỗng KHÔNG được coi là 0 rồi so với ngưỡng 0.

    Nếu ai đó "sửa" `_instant_scalar` cho trả 0.0 thay vì None, rule ngưỡng 0 sẽ
    không bắn (0 > 0 sai) nhưng rule ngưỡng âm sẽ bắn liên tục trên cụm khoẻ.
    """
    await _patched(monkeypatch, {"status": "success", "data": {"result": []}})
    ctx = _Ctx(settings=SimpleNamespace())
    rule = proactive_observer.ProactiveRule(name="x", promql="sum(nothing)", threshold=0.0)
    assert await proactive_observer._evaluate_one_proactive_rule(ctx, rule) == 0
