"""TDD: phán quyết 3 nhánh trên Telegram + sổ ca (case ledger).

Bug được đóng ở đây: trước đó mọi lần bấm nút đều học ``accepted=True``, tức là hệ
thống học từ SỰ CHÚ Ý và coi đó là SỰ ĐỒNG TÌNH — nhánh FROZEN của advisory_promoter
là code chết không đường nào chạm tới.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from services.learning_promoter.advisory_promoter import FROZEN, GRADUATED
from workers.advisory_ack import (
    VERDICT_CORRECT,
    VERDICT_INCORRECT,
    VERDICT_PARTIAL,
    build_advisory_ack_keyboard,
    handle_advisory_ack_callback,
    open_advisory_case,
    parse_advisory_ack_callback,
    parse_advisory_verdict_callback,
)
from workers.unified_incident_card import render_recurrence_notice

# --------------------------------------------------------------------------- fakes


class FakeConn:
    """Đủ SQL của CaseLedgerStore, giữ dữ liệu trong dict — không mock từng lời gọi."""

    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, sql: str, *args):
        sql = " ".join(sql.split())
        if "SET recurred = TRUE" in sql:
            row = self.rows.get(args[0])
            if row is not None and not row["recurred"]:
                row["recurred"] = True
            return "UPDATE 1"
        if "pg_advisory_xact_lock" in sql:
            return "OK"  # khoá chống race occurrence_no — fake chỉ cần nuốt
        raise AssertionError(f"SQL khong duoc fake: {sql[:60]}")

    async def fetchrow(self, sql: str, *args):
        sql = " ".join(sql.split())
        if sql.startswith("SELECT case_id, occurrence_no"):
            tenant, pattern = args
            hits = [
                r for r in self.rows.values()
                if r["tenant_id"] == tenant and r["pattern_key"] == pattern
            ]
            hits.sort(key=lambda r: r["opened_at"], reverse=True)
            return hits[0] if hits else None
        if sql.startswith("INSERT INTO omni_admin.case_ledger"):
            (case_id, tenant_id, pattern_key, lane, alertname, posture,
             occurrence_no, prior_case_id, crat_ref) = args
            if case_id in self.rows:  # ON CONFLICT DO NOTHING
                return None
            self.rows[case_id] = {
                "case_id": case_id, "tenant_id": tenant_id, "pattern_key": pattern_key,
                "lane": lane, "alertname": alertname, "posture": posture,
                "occurrence_no": occurrence_no, "prior_case_id": prior_case_id,
                "crat_ref": crat_ref, "opened_at": datetime.now(timezone.utc),
                "diagnosis_verdict": None, "remedy_verdict": None,
                "diagnosis_source": None, "diagnosis_actor": None,
                "remedy_source": None, "remedy_actor": None, "recurred": False,
            }
            return self.rows[case_id]
        if sql.startswith("UPDATE omni_admin.case_ledger SET diagnosis_verdict"):
            case_id, diagnosis, remedy, source, actor, crat_ref = args
            row = self.rows.get(case_id)
            if row is None:
                return None
            row["diagnosis_verdict"] = diagnosis or row["diagnosis_verdict"]
            row["remedy_verdict"] = remedy or row["remedy_verdict"]
            if diagnosis is not None:
                row["diagnosis_source"] = source
                row["diagnosis_actor"] = actor
            if remedy is not None:
                row["remedy_source"] = source
                row["remedy_actor"] = actor
            return row
        if sql.startswith("SELECT * FROM omni_admin.case_ledger WHERE case_id"):
            return self.rows.get(args[0])
        if sql.startswith("SELECT * FROM omni_admin.case_ledger WHERE tenant_id"):
            tenant, pattern = args
            hits = [
                r for r in self.rows.values()
                if r["tenant_id"] == tenant and r["pattern_key"] == pattern
            ]
            hits.sort(key=lambda r: r["opened_at"], reverse=True)
            return hits[0] if hits else None
        if "pg_advisory_xact_lock" in sql:
            return "OK"  # khoá chống race occurrence_no — fake chỉ cần nuốt
        raise AssertionError(f"SQL khong duoc fake: {sql[:60]}")


class FakePool:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self.rows)


class FakeRepo:
    """Bộ đếm tốt nghiệp trong RAM, đủ để chứng minh đường đi tới FROZEN."""

    def __init__(self) -> None:
        self.counters: dict[str, dict] = {}
        self.states: dict[str, str] = {}
        self.acks: list[dict] = []

    async def record_advisory_acknowledgment(self, **kw):
        self.acks.append(kw)

    async def bump_playbook_graduation(self, *, tenant_id, domain, playbook_id, success, track="playbook"):
        row = self.counters.setdefault(
            playbook_id, {"success_count": 0, "fail_count": 0, "state": "DRAFT"}
        )
        row["success_count" if success else "fail_count"] += 1
        return dict(row)

    async def set_playbook_graduation_state(self, *, tenant_id, domain, playbook_id, state, track="playbook"):
        self.states[playbook_id] = state
        self.counters[playbook_id]["state"] = state


def _ctx(pool=None, repo=None, redis=None):
    return SimpleNamespace(
        settings=SimpleNamespace(
            kafka_topic_advisory_suggestions="omni-advisory-suggestions",
            kafka_topic_audit_chain="omni-audit-chain",
            omni_advisory_graduation_min_success=3,
            omni_advisory_graduation_max_fail_rate=0.25,
        ),
        kafka=AsyncMock(),
        redis=redis if redis is not None else FakeRedis(decode_responses=True),
        telegram=AsyncMock(),
        admin_repo=repo,
        admin_pool=pool,
        current_tenant_id="default",
    )


def _update(data: str, actor: int = 555):
    return {"callback_query": {"id": "cq1", "data": data, "from": {"id": actor}}}


async def _seed_advisory_shape(redis, trace_id: str):
    await redis.set(
        f"omni:trace:advisory:{trace_id}",
        json.dumps({"lane": "resource", "advisory": {"affected_workload": {"alertname": "OOM"}}}),
    )


# --------------------------------------------------------------------------- callback


class TestVerdictKeyboard:
    def test_three_verdict_buttons(self):
        kb = build_advisory_ack_keyboard("trace-1")
        cbs = [b["callback_data"] for b in kb["inline_keyboard"][0]]
        assert cbs == ["advack:ok:trace-1", "advack:bad:trace-1", "advack:part:trace-1"]

    @pytest.mark.parametrize(
        "data,expected",
        [
            ("advack:ok:trace-1", (("trace-1"), VERDICT_CORRECT)),
            ("advack:bad:trace-1", ("trace-1", VERDICT_INCORRECT)),
            ("advack:part:trace-1", ("trace-1", VERDICT_PARTIAL)),
        ],
    )
    def test_parse_verdict(self, data, expected):
        assert parse_advisory_verdict_callback(data) == expected

    def test_legacy_single_button_has_no_verdict(self):
        assert parse_advisory_verdict_callback("advack:trace-1") == ("trace-1", None)
        assert parse_advisory_ack_callback("advack:ok:trace-1") == "trace-1"

    def test_other_namespaces_rejected(self):
        assert parse_advisory_verdict_callback("hitl:approve:x") is None
        assert parse_advisory_verdict_callback("advack:") is None


# --------------------------------------------------------------------------- học


@pytest.mark.asyncio
class TestVerdictDrivesLearning:
    async def test_sai_dan_toi_accepted_false_va_frozen(self, monkeypatch):
        """Bằng chứng bug đã đóng: bấm "Sai" phải đẩy pattern về FROZEN."""
        monkeypatch.setattr("workers.advisory_ack.write_audit_block", AsyncMock())
        pool, repo = FakePool(), FakeRepo()
        ctx = _ctx(pool=pool, repo=repo)
        await _seed_advisory_shape(ctx.redis, "trace-1")

        memory = await open_advisory_case(
            ctx, trace_id="trace-1", tenant_id="default", lane="resource", alertname="OOM",
        )
        pattern = memory["pattern_key"]

        assert await handle_advisory_ack_callback(ctx, _update("advack:bad:trace-1")) is True

        assert repo.counters[pattern]["fail_count"] == 1
        assert repo.counters[pattern]["success_count"] == 0
        assert repo.states[pattern] == FROZEN
        assert pool.rows["trace-1"]["diagnosis_verdict"] == VERDICT_INCORRECT
        assert pool.rows["trace-1"]["diagnosis_source"] == "telegram"
        assert pool.rows["trace-1"]["diagnosis_actor"] == "555"

    async def test_dung_ba_lan_thi_tot_nghiep(self, monkeypatch):
        monkeypatch.setattr("workers.advisory_ack.write_audit_block", AsyncMock())
        pool, repo = FakePool(), FakeRepo()
        ctx = _ctx(pool=pool, repo=repo)
        pattern = ""
        for i in range(3):
            trace = f"trace-{i}"
            await _seed_advisory_shape(ctx.redis, trace)
            mem = await open_advisory_case(
                ctx, trace_id=trace, tenant_id="default", lane="resource", alertname="OOM",
            )
            pattern = mem["pattern_key"]
            await handle_advisory_ack_callback(ctx, _update(f"advack:ok:{trace}"))
        assert repo.counters[pattern]["success_count"] == 3
        assert repo.states[pattern] == GRADUATED

    async def test_partial_khong_thuong_khong_phat(self, monkeypatch):
        monkeypatch.setattr("workers.advisory_ack.write_audit_block", AsyncMock())
        pool, repo = FakePool(), FakeRepo()
        ctx = _ctx(pool=pool, repo=repo)
        await open_advisory_case(
            ctx, trace_id="trace-1", tenant_id="default", lane="resource", alertname="OOM",
        )
        await handle_advisory_ack_callback(ctx, _update("advack:part:trace-1"))
        assert repo.counters == {}
        assert pool.rows["trace-1"]["diagnosis_verdict"] == VERDICT_PARTIAL

    async def test_im_lang_khong_ghi_gi(self, monkeypatch):
        """Không bấm gì = không có ca nào được phán quyết. Không có timeout tự coi là đúng."""
        monkeypatch.setattr("workers.advisory_ack.write_audit_block", AsyncMock())
        pool, repo = FakePool(), FakeRepo()
        ctx = _ctx(pool=pool, repo=repo)
        await open_advisory_case(
            ctx, trace_id="trace-1", tenant_id="default", lane="resource", alertname="OOM",
        )
        assert pool.rows["trace-1"]["diagnosis_verdict"] is None
        assert repo.counters == {}

    async def test_callback_cu_khong_suy_ra_phan_quyet(self, monkeypatch):
        monkeypatch.setattr("workers.advisory_ack.write_audit_block", AsyncMock())
        pool, repo = FakePool(), FakeRepo()
        ctx = _ctx(pool=pool, repo=repo)
        await open_advisory_case(
            ctx, trace_id="trace-1", tenant_id="default", lane="resource", alertname="OOM",
        )
        assert await handle_advisory_ack_callback(ctx, _update("advack:trace-1")) is True
        assert pool.rows["trace-1"]["diagnosis_verdict"] is None
        assert repo.counters == {}

    async def test_sai_khong_tinh_vao_kpi_acceptance(self, monkeypatch):
        monkeypatch.setattr("workers.advisory_ack.write_audit_block", AsyncMock())
        recorded: list[str] = []

        class _KPI:
            def __init__(self, redis):
                pass

            async def record_accepted(self, trace_id, tenant_id="default"):
                recorded.append(trace_id)

        monkeypatch.setattr("workers.kpi_metrics.KPIStore", _KPI)
        ctx = _ctx(pool=FakePool(), repo=FakeRepo())
        await handle_advisory_ack_callback(ctx, _update("advack:bad:trace-1"))
        assert recorded == []
        await handle_advisory_ack_callback(ctx, _update("advack:ok:trace-2"))
        assert recorded == ["trace-2"]


# --------------------------------------------------------------------------- sổ ca


@pytest.mark.asyncio
class TestCaseLedgerBestEffort:
    async def test_khong_co_pool_van_chay(self):
        ctx = _ctx(pool=None, repo=FakeRepo())
        assert await open_advisory_case(
            ctx, trace_id="t1", tenant_id="default", lane="resource", alertname="OOM",
        ) == {}

    async def test_pool_hong_khong_chan_advisory(self, monkeypatch):
        class BoomPool:
            @asynccontextmanager
            async def acquire(self):
                raise RuntimeError("pg down")
                yield  # pragma: no cover

        ctx = _ctx(pool=BoomPool(), repo=FakeRepo())
        assert await open_advisory_case(
            ctx, trace_id="t1", tenant_id="default", lane="resource", alertname="OOM",
        ) == {}

        monkeypatch.setattr("workers.advisory_ack.write_audit_block", AsyncMock())
        assert await handle_advisory_ack_callback(ctx, _update("advack:ok:t1")) is True

    async def test_pattern_rong_thi_khong_mo_ca(self):
        ctx = _ctx(pool=FakePool(), repo=FakeRepo())
        assert await open_advisory_case(
            ctx, trace_id="t1", tenant_id="default", lane="", alertname="",
        ) == {}


# --------------------------------------------------------------------------- trí nhớ


@pytest.mark.asyncio
class TestMemory:
    async def test_lan_2_biet_lan_1(self):
        ctx = _ctx(pool=FakePool(), repo=FakeRepo())
        first = await open_advisory_case(
            ctx, trace_id="t1", tenant_id="default", lane="resource", alertname="OOM",
        )
        assert first["occurrence_no"] == 1
        assert "prior_case_id" not in first

        second = await open_advisory_case(
            ctx, trace_id="t2", tenant_id="default", lane="resource", alertname="OOM",
        )
        assert second["occurrence_no"] == 2
        assert second["prior_case_id"] == "t1"
        assert second["prior_diagnosis_verdict"] is None

    async def test_tenant_khac_khong_dung_chung_tri_nho(self):
        ctx = _ctx(pool=FakePool(), repo=FakeRepo())
        await open_advisory_case(
            ctx, trace_id="t1", tenant_id="acme", lane="resource", alertname="OOM",
        )
        other = await open_advisory_case(
            ctx, trace_id="t2", tenant_id="globex", lane="resource", alertname="OOM",
        )
        assert other["occurrence_no"] == 1

    async def test_mo_lai_cung_case_id_khong_tang_so_lan(self):
        ctx = _ctx(pool=FakePool(), repo=FakeRepo())
        await open_advisory_case(
            ctx, trace_id="t1", tenant_id="default", lane="resource", alertname="OOM",
        )
        again = await open_advisory_case(
            ctx, trace_id="t1", tenant_id="default", lane="resource", alertname="OOM",
        )
        assert again["occurrence_no"] == 1


@pytest.mark.asyncio
class TestEmitterWiring:
    async def test_lan_2_the_telegram_noi_ro_lan_thu_2(self):
        """Thẻ lần 2 phải khác thẻ lần 1 — và marker máy vẫn nguyên vẹn."""
        from tests.test_telegram_chunk_boundary import make_advisory
        from workers.telegram_advisory_emitter import render_advisory_to_telegram

        pool = FakePool()
        ctx = _ctx(pool=pool, repo=FakeRepo())
        ctx.telegram.send_message = AsyncMock(return_value={"result": {"message_id": 1}})

        first = make_advisory()
        await render_advisory_to_telegram(ctx, first, chat_id=1, lane_label="resource")
        msg1 = ctx.telegram.send_message.call_args[0][1]
        assert "Trí nhớ" not in msg1

        second = make_advisory()
        object.__setattr__(second, "trace_id", "trace-test-002")
        await render_advisory_to_telegram(ctx, second, chat_id=1, lane_label="resource")
        msg2 = ctx.telegram.send_message.call_args[0][1]
        assert "Trí nhớ" in msg2 and "lần thứ 2" in msg2
        assert "CHƯA có ai phán quyết" in msg2
        assert "*TRACE:*" in msg2

    async def test_khong_co_pool_the_van_gui_binh_thuong(self):
        from tests.test_telegram_chunk_boundary import make_advisory
        from workers.telegram_advisory_emitter import render_advisory_to_telegram

        ctx = _ctx(pool=None, repo=FakeRepo())
        ctx.telegram.send_message = AsyncMock(return_value={"result": {"message_id": 1}})
        await render_advisory_to_telegram(ctx, make_advisory(), chat_id=1, lane_label="resource")
        ctx.telegram.send_message.assert_awaited_once()


class TestRecurrenceNotice:
    def test_lan_dau_khong_hien_gi(self):
        assert render_recurrence_notice({"occurrence_no": 1}) == ""
        assert render_recurrence_notice({}) == ""
        assert render_recurrence_notice(None) == ""

    def test_lan_2_noi_ro_da_bao_va_chua_ai_phan_quyet(self):
        out = render_recurrence_notice({
            "occurrence_no": 2,
            "prior_case_id": "trace-old",
            "prior_opened_at": "2026-07-28 10:00",
            "prior_diagnosis_verdict": None,
        })
        assert "lần thứ 2" in out
        assert "2026-07-28" in out
        assert "CHƯA có ai phán quyết" in out
        assert "không phải phát hiện mới" in out

    def test_muc_khan_tang_theo_so_lan(self):
        n3 = render_recurrence_notice({"occurrence_no": 3})
        n5 = render_recurrence_notice({"occurrence_no": 5})
        assert "⚠️" in n3 and "🚨" not in n3
        assert "🚨" in n5 and "nguyên nhân gốc" in n5

    def test_ca_truoc_da_bi_danh_gia_sai(self):
        out = render_recurrence_notice({
            "occurrence_no": 2, "prior_diagnosis_verdict": "INCORRECT", "prior_recurred": True,
        })
        assert "đã bị đánh giá SAI" in out
        assert "tái diễn" in out

    def test_khong_dung_marker_may(self):
        """WHAT/WHO/WHY/HOW-TO là marker parse-coupled — section trí nhớ không được đụng."""
        out = render_recurrence_notice({"occurrence_no": 4})
        for marker in ("WHAT", "WHO", "WHY", "HOW-TO"):
            assert marker not in out
