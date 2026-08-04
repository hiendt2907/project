"""#28 — nhánh chẩn đoán CHÍNH (emit_diagnosis_to_telegram) phải mở case_ledger
+ gắn ack-keyboard, giống hệt pattern đã hoạt động ở
``telegram_advisory_emitter.render_advisory_to_telegram``.

Ground truth 2026-08-04: audit chain có 1003+ block ADVISORY_DECISION nhưng
case_ledger chỉ từng có 2 dòng, vì nhánh CHÍNH (mọi cluster critical/high đi
qua đây) gửi Telegram trần, không nút, không mở case. Test này khoá lại: case
phải mở TRƯỚC khi gửi, và ack-keyboard phải nằm trên chunk CUỐI (kể cả khi
message dài phải chia nhiều tin).
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from workers.remote_diagnosis_emitter import emit_diagnosis_to_telegram


class _FakeConn:
    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows

    @contextlib.asynccontextmanager
    async def _tx(self):
        yield None

    def transaction(self):
        return self._tx()

    async def execute(self, sql: str, *args):
        return "OK"

    async def fetch(self, sql: str, *args):
        # last_case_for_pattern's dual-key lookup — không có ca trước trong test này.
        return []

    async def fetchrow(self, sql: str, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT case_id, occurrence_no"):
            tenant, pattern = args
            cands = [
                r for r in self.rows.values()
                if r["tenant_id"] == tenant and r["pattern_key"] == pattern
            ]
            return cands[-1] if cands else None
        if s.startswith("INSERT INTO omni_admin.case_ledger"):
            (case_id, tenant_id, pattern_key, lane, alertname, posture,
             occurrence_no, prior_case_id, crat_ref) = args
            if case_id in self.rows:
                return None
            self.rows[case_id] = {
                "case_id": case_id, "tenant_id": tenant_id, "pattern_key": pattern_key,
                "lane": lane, "alertname": alertname, "posture": posture,
                "occurrence_no": occurrence_no, "prior_case_id": prior_case_id,
                "crat_ref": crat_ref, "diagnosis_verdict": "UNJUDGED",
                "remedy_verdict": "UNJUDGED",
            }
            return dict(self.rows[case_id])
        raise AssertionError(f"SQL ngoai du kien: {s[:80]}")


class _FakePool:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield _FakeConn(self.rows)


class _FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, *, reply_markup=None, parse_mode=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"result": {"message_id": len(self.sent)}}


def _session(*, root_cause="disk 96% on /var", turns=1) -> dict:
    return {
        "trace_id": "ra-diag-1",
        "agent_id": "staging-sim_cust-app",
        "probe": "remote_system_metrics",
        "lane": "SYS_RESOURCE",
        "total_turns": turns,
        "turns": [
            {
                "turn": i + 1,
                "hypothesis": f"h{i}",
                "command_results": [{
                    "command_str": f"cmd-{i}", "purpose": "check disk", "rc": 0,
                    "stdout": "line\n" * 40,  # đủ dài để buộc render vượt ngưỡng chunk
                }],
            }
            for i in range(turns)
        ],
        "final": {
            "root_cause": root_cause, "confidence": 0.8,
            "affected_components": ["disk"], "remediation_steps": ["clear /var/log"],
        },
    }


def _ctx(pool):
    return SimpleNamespace(
        telegram=_FakeTelegram(),
        admin_pool=pool,
        settings=SimpleNamespace(telegram_send_timeout_sec=5.0),
    )


async def test_emit_diagnosis_opens_case_before_sending():
    pool = _FakePool()
    ctx = _ctx(pool)

    await emit_diagnosis_to_telegram(ctx, _session(), 12345, tenant_id="acme")

    assert "ra-diag-1" in pool.rows, "nhánh chẩn đoán chính vẫn chưa mở case (#28 chưa đóng)"
    row = pool.rows["ra-diag-1"]
    assert row["tenant_id"] == "acme"
    assert row["lane"] == "SYS_RESOURCE"
    assert row["alertname"] == "remote_system_metrics"


async def test_emit_diagnosis_attaches_ack_keyboard_to_message():
    pool = _FakePool()
    ctx = _ctx(pool)

    await emit_diagnosis_to_telegram(ctx, _session(), 12345, tenant_id="acme")

    assert len(ctx.telegram.sent) == 1
    reply_markup = ctx.telegram.sent[0]["reply_markup"]
    assert reply_markup is not None, "trước bản vá: tin nhắn trần, không nút phản hồi nào"
    callback_datas = [
        btn["callback_data"]
        for row in reply_markup["inline_keyboard"]
        for btn in row
    ]
    assert any("ra-diag-1" in cd for cd in callback_datas)


async def test_emit_diagnosis_attaches_ack_keyboard_only_to_last_chunk():
    pool = _FakePool()
    ctx = _ctx(pool)

    await emit_diagnosis_to_telegram(ctx, _session(turns=60), 12345, tenant_id="acme")

    assert len(ctx.telegram.sent) >= 2, "test setup phải tạo ra >=2 chunk"
    for msg in ctx.telegram.sent[:-1]:
        assert msg["reply_markup"] is None
    assert ctx.telegram.sent[-1]["reply_markup"] is not None


async def test_emit_diagnosis_survives_missing_case_store():
    """admin_pool=None (lab chưa cấu hình OMNI_ADMIN_PG_DSN) không được chặn
    đường gửi Telegram — mở case là best-effort tuyệt đối."""
    ctx = _ctx(None)

    await emit_diagnosis_to_telegram(ctx, _session(), 12345, tenant_id="acme")

    assert len(ctx.telegram.sent) == 1
    assert ctx.telegram.sent[0]["reply_markup"] is not None
