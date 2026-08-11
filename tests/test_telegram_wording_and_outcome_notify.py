"""Đ53 — fix chân thẻ Telegram sai + đóng vòng khép kín bằng thông báo kết quả.

Hai lỗi tìm được khi kiểm chứng vòng tự khắc phục sáng nay (Đ52):

1. `_render_section4_remediation` LUÔN in "Omni không tự thực thi" và "Mọi thay đổi
   cần approval" — đúng ở tier `shadow` nhưng SAI kể từ khi tier `assist` + auto-execute
   được bật (payment-api đã được Omni tự khởi động lại thật, không cần người duyệt,
   xác nhận `COMPLETED rc=0 verified=True` trên UAT). Người vận hành đọc thẻ này sẽ tin
   là phải tự tay chạy lệnh trong khi Omni có thể đã làm xong.

2. Sau khi lệnh tự khắc phục kết thúc (`remote_command_outcome_loop.reconcile_one`),
   KHÔNG có thông báo Telegram nào — người vận hành chỉ thấy thẻ chẩn đoán ban đầu rồi
   im lặng, dù `_write_outcome_audit`/`_upsert_action_experience` đã chạy xong đầy đủ.
   Xác nhận trên Redis thật: `omni:cmd:rec:loyalty-uat:cmd-d79afbcf3e1c4b1f` có
   `state=COMPLETED verified=True` lúc 13:51:21 nhưng không kênh nào báo cho người dùng.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from workers.remote_diagnosis_emitter import render_diagnosis_session


# ── 1. Chân thẻ không còn khẳng định sai ─────────────────────────────────────

def _session(**over) -> dict:
    base = {
        "trace_id": "ra-abc123", "agent_id": "loyalty-uat_cust-app",
        "probe": "service_systemd_units", "domain": "service",
        "alert_hint": "payment-api dừng",
        "turns": [],
        "final": {
            "root_cause": "payment-api bị dừng", "confidence": 0.9,
            "affected_components": ["payment-api"],
            "remediation_steps": ["sudo systemctl start payment-api"],
        },
    }
    base.update(over)
    return base


def test_khong_con_khang_dinh_omni_khong_tu_thuc_thi():
    """Câu khẳng định tuyệt đối này SAI ở tier assist/auto — phải bỏ."""
    text = render_diagnosis_session(_session())
    assert "Omni không tự thực thi" not in text
    assert "Mọi thay đổi cần approval trước khi thực thi" not in text


def test_van_giu_nhan_day_la_de_xuat_khong_phai_da_hoan_tat():
    """Không được lật sang cực đoan ngược — thẻ CHẨN ĐOÁN vẫn phát trước khi biết
    kết quả thực thi (auto-recovery chạy SAU khi Telegram đã emit), nên không được
    khẳng định "đã xong". Chỉ được nói đúng nhất có thể tại thời điểm phát thẻ."""
    text = render_diagnosis_session(_session())
    lines = text.lower()
    assert "cần làm" in lines  # vẫn liệt kê remediation_steps
    assert "đã khắc phục xong" not in lines
    assert "đã hoàn tất" not in lines


def test_khong_co_remediation_steps_van_khong_bao_sai():
    text = render_diagnosis_session(_session(final={
        "root_cause": "chưa rõ", "confidence": 0.0,
        "affected_components": [], "remediation_steps": [],
    }))
    assert "Omni không tự thực thi" not in text


# ── 2. Vòng khép kín: thông báo KẾT QUẢ tự khắc phục qua Telegram ───────────

class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, ex=None, **kw):
        self.kv[key] = value
        return True

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zrange(self, key, start, end):
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        return [m for m, _ in items[start:end + 1 if end >= 0 else None]]

    async def zscore(self, key, member):
        return self.zsets.get(key, {}).get(member)

    async def zrem(self, key, member):
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0


class FakeKafka:
    async def send_dict(self, topic, msg, key=None):
        pass


class FakeLLM:
    async def embed(self, model: str, input: str) -> dict:
        return {"embedding": [0.2] * 768}


class FakeVectorStore:
    async def upsert(self, *, collection_name: str, points: list[Any]) -> None:
        pass


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return {"ok": True}


def _settings():
    return SimpleNamespace(
        omni_gateway_api_key="k", omni_gateway_internal_url="http://gw.local",
        kafka_topic_audit_chain="omni-audit-chain",
        kafka_topic_action_feedback="omni-action-feedback",
        embed_model="nomic-embed-text:latest", memory_canonical_strip_pods=True,
        telegram_admin_chat_id=None,
    )


@pytest.fixture
def ctx():
    return SimpleNamespace(
        redis=FakeRedis(), kafka=FakeKafka(), settings=_settings(),
        llm=FakeLLM(), vector_store=FakeVectorStore(), telegram=FakeTelegram(),
    )


@pytest.fixture
def audit_ok(monkeypatch):
    async def _write(**kwargs):
        return {"block_hash": "deadbeef"}

    monkeypatch.setattr("services.audit_ledger.chain_writer.write_audit_block", _write)
    return []


def _seed(ctx, *, state="COMPLETED", rc=0, chat_id=None, tenant="loyalty-uat",
          cid="cmd-1", trace="ra-1", unit="payment-api.service"):
    import json
    import time

    from workers import auto_recovery_bridge as arb

    ctx.redis.kv[f"omni:cmd:rec:{tenant}:{cid}"] = json.dumps({
        "command_id": cid, "agent_id": "loyalty-uat_cust-app", "state": state,
        "delivery_attempt": 1, "action_id": "act-1", "canonical_scope": f"{tenant}:svc:x",
        "incident_id": trace,
        "outcome": {"status": "recovered" if rc == 0 else "aborted", "rc": rc,
                    "reason": "service + dependents verified" if rc == 0 else "executor_exception: boom",
                    "evidence": ["before=inactive", "service_health=ok"],
                    "verified": rc == 0},
    })
    meta = {"trace_id": trace, "agent_id": "loyalty-uat_cust-app", "unit": unit,
            "capability": "systemd.restart_unit"}
    if chat_id is not None:
        meta["chat_id"] = chat_id
    ctx.redis.kv[f"omni:autorecovery:meta:{tenant}:{cid}"] = json.dumps(meta)
    ctx.redis.zsets.setdefault(arb.PENDING_KEY, {})[arb.pending_member(tenant, cid)] = time.time()


async def test_thanh_cong_thi_bao_qua_telegram(ctx, audit_ok):
    from workers import remote_command_outcome_loop as rcol

    _seed(ctx, state="COMPLETED", rc=0, chat_id=555)
    assert await rcol.reconcile_one(ctx, "loyalty-uat", "cmd-1") == "done"

    assert len(ctx.telegram.sent) == 1
    chat_id, text = ctx.telegram.sent[0]
    assert chat_id == 555
    assert "payment-api" in text
    # Phải phân biệt được với thẻ chẩn đoán ban đầu — đây là tin BÁO KẾT QUẢ.
    assert any(w in text for w in ("✅", "tự khắc phục", "COMPLETED", "thành công"))


async def test_that_bai_cung_phai_bao_khong_duoc_im_lang(ctx, audit_ok):
    """Im lặng khi thất bại còn tệ hơn im lặng khi thành công — người vận hành
    tưởng Omni đang xử lý trong khi thực ra nó đã bỏ cuộc từ lâu."""
    from workers import remote_command_outcome_loop as rcol

    _seed(ctx, state="FAILED", rc=1, chat_id=555)
    assert await rcol.reconcile_one(ctx, "loyalty-uat", "cmd-1") == "done"

    assert len(ctx.telegram.sent) == 1
    _, text = ctx.telegram.sent[0]
    assert any(w in text for w in ("❌", "thất bại", "FAILED", "không"))
    assert "boom" in text or "executor_exception" in text


async def test_khong_co_chat_id_thi_khong_gui_khong_loi(ctx, audit_ok):
    """Ca cũ / dispatch qua đường không biết chat_id (vd known-fix reflex) — không
    được nổ, chỉ đơn giản là không gửi được thông báo theo dõi."""
    from workers import remote_command_outcome_loop as rcol

    _seed(ctx, state="COMPLETED", rc=0, chat_id=None)
    assert await rcol.reconcile_one(ctx, "loyalty-uat", "cmd-1") == "done"
    assert ctx.telegram.sent == []


async def test_telegram_gui_loi_khong_lam_hong_reconcile(ctx, audit_ok):
    """Best-effort: lỗi gửi Telegram không được biến một outcome đã CRAT-audit
    thành công thành 'retry' — bài học đã ghi, audit đã ký, đừng lặp vì một API lỗi."""
    from workers import remote_command_outcome_loop as rcol

    async def _boom(*a, **k):
        raise RuntimeError("telegram 500")
    ctx.telegram.send_message = _boom

    _seed(ctx, state="COMPLETED", rc=0, chat_id=555)
    assert await rcol.reconcile_one(ctx, "loyalty-uat", "cmd-1") == "done"


async def test_khong_co_telegram_client_thi_bo_qua_sach(audit_ok):
    """`ctx.telegram is None` (lab không cấu hình bot) vẫn phải reconcile bình
    thường — thông báo là tiện ích thêm, không phải điều kiện tiên quyết."""
    from workers import remote_command_outcome_loop as rcol

    ctx = SimpleNamespace(
        redis=FakeRedis(), kafka=FakeKafka(), settings=_settings(),
        llm=FakeLLM(), vector_store=FakeVectorStore(), telegram=None,
    )
    _seed(ctx, state="COMPLETED", rc=0, chat_id=555)
    assert await rcol.reconcile_one(ctx, "loyalty-uat", "cmd-1") == "done"


# ── 3. chat_id phải được truyền xuyên suốt dispatch → meta ───────────────────

async def test_dispatch_if_eligible_luu_chat_id_vao_meta(monkeypatch):
    """Không lưu chat_id lúc dispatch thì reconcile không bao giờ biết gửi đi đâu —
    đây là mắt xích còn thiếu khiến vòng khép kín bị câm ở bước cuối."""
    import json

    from workers import auto_recovery_bridge as arb

    redis = FakeRedis()
    await arb.register_pending_command(
        redis, tenant_id="loyalty-uat", command_id="cmd-9", trace_id="ra-9",
        agent_id="loyalty-uat_cust-app", unit="payment-api.service",
        capability="systemd.restart_unit", chat_id=777,
    )
    meta = json.loads(redis.kv["omni:autorecovery:meta:loyalty-uat:cmd-9"])
    assert meta["chat_id"] == 777


async def test_register_pending_command_khong_chat_id_van_hoat_dong_nhu_cu():
    """Tương thích ngược: call site cũ (known-fix reflex) chưa truyền chat_id."""
    import json

    from workers import auto_recovery_bridge as arb

    redis = FakeRedis()
    await arb.register_pending_command(
        redis, tenant_id="loyalty-uat", command_id="cmd-9", trace_id="ra-9",
        agent_id="loyalty-uat_cust-app", unit="payment-api.service",
        capability="systemd.restart_unit",
    )
    meta = json.loads(redis.kv["omni:autorecovery:meta:loyalty-uat:cmd-9"])
    assert "chat_id" not in meta
