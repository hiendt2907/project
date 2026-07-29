"""Test tính chất cho `case_ledger.store` với một fake asyncpg viết tay.

Vì sao fake tự viết chứ không AsyncMock: các tính chất cần chứng minh ở đây là
tính chất của DỮ LIỆU sau nhiều lần gọi (occurrence_no tăng dần, ON CONFLICT
không ghi đè). AsyncMock chỉ ghi lại lời gọi nên sẽ "xanh" ngay cả khi câu lệnh
sai hoàn toàn — âm tính giả đúng ở chỗ nguy hiểm nhất.

Fake dưới đây mô phỏng đủ ngữ nghĩa asyncpg mà store dùng: ON CONFLICT DO NOTHING
trả về None, RETURNING trả hàng, và UPDATE ... WHERE không khớp trả None.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from services.case_ledger.store import CaseLedgerStore


# ── Fake asyncpg ─────────────────────────────────────────────────────────────


class FakeConn:
    """Diễn giải đúng các câu lệnh mà CaseLedgerStore phát ra, trên dict trong RAM."""

    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows
        self.sql_log: list[str] = []
        self.tx_depth = 0

    @asynccontextmanager
    async def transaction(self):
        self.tx_depth += 1
        try:
            yield self
        finally:
            self.tx_depth -= 1

    def _norm(self, sql: str) -> str:
        return " ".join(sql.split())

    async def fetchrow(self, sql: str, *args):
        sql = self._norm(sql)
        self.sql_log.append(sql)

        if sql.startswith("SELECT case_id, occurrence_no"):
            tenant_id, pattern_key = args
            same = [
                r for r in self.rows.values()
                if r["tenant_id"] == tenant_id and r["pattern_key"] == pattern_key
            ]
            if not same:
                return None
            return max(same, key=lambda r: r["opened_at"])

        if sql.startswith("INSERT INTO omni_admin.case_ledger"):
            (case_id, tenant_id, pattern_key, lane, alertname, posture,
             occurrence_no, prior_case_id, crat_ref) = args
            if case_id in self.rows:
                return None  # ON CONFLICT DO NOTHING → không có RETURNING
            self.rows[case_id] = {
                "case_id": case_id,
                "tenant_id": tenant_id,
                "pattern_key": pattern_key,
                "lane": lane,
                "alertname": alertname,
                "posture": posture,
                "occurrence_no": occurrence_no,
                "prior_case_id": prior_case_id,
                "crat_ref": crat_ref,
                "diagnosis_verdict": "UNJUDGED",
                "remedy_verdict": "UNJUDGED",
                "diagnosis_source": None, "diagnosis_actor": None,
                "remedy_source": None, "remedy_actor": None,
                "recurred": False,
                "opened_at": len(self.rows),  # thứ tự chèn = thứ tự thời gian
            }
            return dict(self.rows[case_id])

        if sql.startswith("SELECT * FROM omni_admin.case_ledger WHERE case_id"):
            row = self.rows.get(args[0])
            return dict(row) if row else None

        if sql.startswith("SELECT * FROM omni_admin.case_ledger WHERE tenant_id"):
            tenant_id, pattern_key = args
            same = [
                r for r in self.rows.values()
                if r["tenant_id"] == tenant_id and r["pattern_key"] == pattern_key
            ]
            if not same:
                return None
            return dict(max(same, key=lambda r: r["opened_at"]))

        if sql.startswith("UPDATE omni_admin.case_ledger SET diagnosis_verdict"):
            case_id, diagnosis, remedy, source, actor, crat_ref = args
            row = self.rows.get(case_id)
            if row is None:
                return None
            if diagnosis is not None:
                row["diagnosis_verdict"] = diagnosis
            if remedy is not None:
                row["remedy_verdict"] = remedy
            if diagnosis is not None:
                row["diagnosis_source"] = source
                row["diagnosis_actor"] = actor
            if remedy is not None:
                row["remedy_source"] = source
                row["remedy_actor"] = actor
            if crat_ref is not None:
                row["crat_ref"] = crat_ref
            return dict(row)

        if sql.startswith("UPDATE omni_admin.case_ledger SET recurred"):
            row = self.rows.get(args[0])
            if row is None or row["recurred"]:
                return None
            row["recurred"] = True
            return {"case_id": row["case_id"]}

        raise AssertionError(f"fake khong hieu SQL: {sql}")

    async def fetch(self, sql: str, *args):
        sql = self._norm(sql)
        self.sql_log.append(sql)
        if sql.startswith("SELECT * FROM omni_admin.case_ledger WHERE tenant_id"):
            tenant_id, pattern_key, limit = args
            same = [
                dict(r) for r in self.rows.values()
                if r["tenant_id"] == tenant_id and r["pattern_key"] == pattern_key
            ]
            same.sort(key=lambda r: r["opened_at"], reverse=True)
            return same[:limit]
        if sql.startswith("SELECT DISTINCT pattern_key"):
            return [
                {"pattern_key": p}
                for p in sorted({
                    r["pattern_key"] for r in self.rows.values()
                    if r["tenant_id"] == args[0]
                })
            ]
        if sql.startswith("SELECT * FROM omni_admin.case_verdict_history"):
            return []
        raise AssertionError(f"fake khong hieu SQL: {sql}")

    async def execute(self, sql: str, *args):
        self.sql_log.append(self._norm(sql))
        return "OK"


class FakePool:
    def __init__(self) -> None:
        self.conn = FakeConn({})

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture
def store() -> CaseLedgerStore:
    return CaseLedgerStore(FakePool())


# ── Trí nhớ: occurrence_no + prior_case_id ───────────────────────────────────


async def test_open_case_tu_tinh_occurrence_no_tang_dan(store):
    """Lần thứ N phải do sổ ca tự đếm, không do caller nhớ truyền vào.

    Nếu occurrence_no đến từ tham số, trí nhớ của Omni phụ thuộc vào chỗ gọi —
    một đường gọi quên truyền là tụt về "lần 1" và toàn bộ tính chất "lần 2 khác
    lần 1" biến mất trong im lặng.
    """
    nos = []
    for i in range(4):
        row = await store.open_case(
            case_id=f"c{i}", tenant_id="t1", pattern_key="OOMKilled:api",
            posture="DIAGNOSED",
        )
        nos.append(row["occurrence_no"])
    assert nos == [1, 2, 3, 4]


async def test_open_case_tro_prior_case_id_dung_ca_truoc_cung_pattern(store):
    """prior_case_id phải trỏ ca trước CÙNG pattern, bỏ qua ca xen giữa khác pattern.

    Đây là cái neo cho câu "tôi đã báo ngày X, nguyên nhân Y". Trỏ nhầm sang ca
    khác loại thì Omni sẽ kể lại một tiền sử không liên quan — tệ hơn là không nhớ.
    """
    await store.open_case(case_id="a1", tenant_id="t1", pattern_key="P", posture="DIAGNOSED")
    await store.open_case(case_id="x1", tenant_id="t1", pattern_key="KHAC", posture="DIAGNOSED")
    a2 = await store.open_case(case_id="a2", tenant_id="t1", pattern_key="P", posture="REFUSED")

    assert a2["prior_case_id"] == "a1"
    assert a2["occurrence_no"] == 2


async def test_open_case_dem_rieng_theo_tenant(store):
    """Tenant khác nhau không được dùng chung trí nhớ — INV_NAMESPACE_ISOLATION."""
    await store.open_case(case_id="t1c1", tenant_id="t1", pattern_key="P", posture="DIAGNOSED")
    row = await store.open_case(
        case_id="t2c1", tenant_id="t2", pattern_key="P", posture="DIAGNOSED"
    )
    assert row["occurrence_no"] == 1
    assert row["prior_case_id"] is None


async def test_ca_dau_tien_khong_co_prior(store):
    row = await store.open_case(
        case_id="c1", tenant_id="t1", pattern_key="P", posture="DIAGNOSED"
    )
    assert row["occurrence_no"] == 1
    assert row["prior_case_id"] is None


# ── Idempotency: gọi lại KHÔNG ghi đè ────────────────────────────────────────


async def test_open_case_goi_lai_cung_case_id_khong_ghi_de(store):
    """ON CONFLICT DO NOTHING — ghi đè sẽ là đường vòng đổi pattern_key không cần UPDATE.

    Trigger DB cấm UPDATE pattern_key/posture. Nếu open_case dùng DO UPDATE thì
    chỉ cần gọi lại với cùng case_id là nắn được nhóm — đúng cái bùa số tinh vi
    nhất mà thiết kế muốn chặn.
    """
    goc = await store.open_case(
        case_id="c1", tenant_id="t1", pattern_key="P_THAT",
        posture="DIAGNOSED", lane="SYS_RESOURCE",
    )
    lai = await store.open_case(
        case_id="c1", tenant_id="t1", pattern_key="P_DEP_HON",
        posture="REFUSED", lane="KHAC",
    )
    assert lai["pattern_key"] == "P_THAT"
    assert lai["posture"] == "DIAGNOSED"
    assert lai["lane"] == "SYS_RESOURCE"
    assert lai == goc


async def test_open_case_goi_lai_khong_lam_phong_occurrence_no(store):
    """Gọi lại không được đẩy "lần thứ mấy" lên — retry/at-least-once là chuyện thường."""
    await store.open_case(case_id="c1", tenant_id="t1", pattern_key="P", posture="DIAGNOSED")
    await store.open_case(case_id="c1", tenant_id="t1", pattern_key="P", posture="DIAGNOSED")
    moi = await store.open_case(
        case_id="c2", tenant_id="t1", pattern_key="P", posture="DIAGNOSED"
    )
    assert moi["occurrence_no"] == 2


async def test_open_case_chay_trong_transaction(store):
    """Đọc-lịch-sử rồi ghi phải nằm trong một transaction, không được tách rời."""
    pool = store._pool
    await store.open_case(case_id="c1", tenant_id="t1", pattern_key="P", posture="DIAGNOSED")
    assert pool.conn.tx_depth == 0  # đã đóng
    assert len(pool.conn.sql_log) >= 2  # SELECT prior + INSERT


# ── Validate đầu vào ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("posture", ["diagnosed", "SKIPPED", "", "IGNORED", "self"])
async def test_open_case_posture_la_phai_ValueError(store, posture):
    """posture lạ phải nổ ngay, không được lọt vào DB rồi bị scoring gom nhầm.

    scoring coi mọi posture không phải REFUSED/OUT_OF_SCOPE là DIAGNOSED — nên một
    posture lạ lọt xuống sẽ âm thầm bị tính vào mẫu số độ chính xác.
    """
    with pytest.raises(ValueError):
        await store.open_case(
            case_id="c1", tenant_id="t1", pattern_key="P", posture=posture
        )


@pytest.mark.parametrize(
    "kw",
    [
        {"case_id": "", "tenant_id": "t1", "pattern_key": "P"},
        {"case_id": "c1", "tenant_id": "", "pattern_key": "P"},
        {"case_id": "c1", "tenant_id": "t1", "pattern_key": ""},
    ],
)
async def test_open_case_thieu_khoa_bat_buoc_la_ValueError(store, kw):
    """Ca không có pattern_key là ca không thuộc nhóm nào — không đo được gì."""
    with pytest.raises(ValueError):
        await store.open_case(posture="DIAGNOSED", **kw)


# ── Người chấm không phải người làm ──────────────────────────────────────────


@pytest.mark.parametrize("source", ["self", "system", "omni", "llm", "", "SELF"])
async def test_record_verdict_tu_choi_source_khong_hop_le(store, source):
    """Không có nguồn 'self'/'system' — người làm không được là người chấm.

    Đây là tinh thần SOX §404 mà CRAT vốn xây theo. Nếu Omni tự chấm được, mọi
    con số năng lực trở thành lời tự khai và bằng chứng trao quyền vô giá trị.
    """
    await store.open_case(case_id="c1", tenant_id="t1", pattern_key="P", posture="DIAGNOSED")
    with pytest.raises(ValueError):
        await store.record_verdict(case_id="c1", source=source, diagnosis="CORRECT")

    ca = await store.get_case("c1")
    assert ca["diagnosis_verdict"] == "UNJUDGED"  # không có ghi lén


@pytest.mark.parametrize("source", ["telegram", "hitl", "portal", "world"])
async def test_record_verdict_chap_nhan_bon_nguon_ngoai(store, source):
    """Bốn nguồn hợp lệ đều phải chạy — hàng rào luôn đóng cũng vô dụng như luôn mở."""
    await store.open_case(case_id="c1", tenant_id="t1", pattern_key="P", posture="DIAGNOSED")
    row = await store.record_verdict(
        case_id="c1", source=source, actor="admin@x", diagnosis="CORRECT"
    )
    assert row["diagnosis_verdict"] == "CORRECT"
    assert row["diagnosis_source"] == source
    assert row["diagnosis_actor"] == "admin@x"


async def test_record_verdict_khong_co_phan_quyet_nao_la_ValueError(store):
    """Gọi mà không truyền nhãn nào chỉ ghi đè verdict_source/actor — im lặng phá dấu vết."""
    await store.open_case(case_id="c1", tenant_id="t1", pattern_key="P", posture="DIAGNOSED")
    with pytest.raises(ValueError):
        await store.record_verdict(case_id="c1", source="portal")


async def test_record_verdict_chi_cham_mot_nhan_khong_dung_nhan_kia(store):
    """Hai nhãn tách rời: chấm remedy không được vô tình đặt lại diagnosis (COALESCE)."""
    await store.open_case(case_id="c1", tenant_id="t1", pattern_key="P", posture="DIAGNOSED")
    await store.record_verdict(case_id="c1", source="hitl", diagnosis="CORRECT")
    row = await store.record_verdict(case_id="c1", source="world", remedy="INCORRECT")
    assert row["diagnosis_verdict"] == "CORRECT"
    assert row["remedy_verdict"] == "INCORRECT"


async def test_record_verdict_ca_khong_ton_tai_tra_dict_rong(store):
    """Không được tạo ca mới từ đường chấm điểm — ca chỉ sinh ra lúc Omni phát biểu."""
    assert await store.record_verdict(
        case_id="khong-co", source="portal", diagnosis="CORRECT"
    ) == {}


# ── Sự thật từ thế giới ──────────────────────────────────────────────────────


async def test_mark_recurred_chi_an_mot_lan(store):
    """Tái diễn là nhãn một chiều; gọi lại phải trả False chứ không đếm trùng."""
    await store.open_case(case_id="c1", tenant_id="t1", pattern_key="P", posture="DIAGNOSED")
    assert await store.mark_recurred(case_id="c1") is True
    assert await store.mark_recurred(case_id="c1") is False
    assert (await store.get_case("c1"))["recurred"] is True


async def test_mark_recurred_ca_khong_ton_tai_tra_False(store):
    assert await store.mark_recurred(case_id="khong-co") is False


# ── Đọc ──────────────────────────────────────────────────────────────────────


async def test_list_cases_for_pattern_loc_dung_va_moi_nhat_truoc(store):
    """Danh sách này là đầu vào của build_competency_report — lọt ca khác pattern là bùa số."""
    for i in range(3):
        await store.open_case(
            case_id=f"p{i}", tenant_id="t1", pattern_key="P", posture="DIAGNOSED"
        )
    await store.open_case(case_id="q0", tenant_id="t1", pattern_key="Q", posture="DIAGNOSED")
    await store.open_case(case_id="z0", tenant_id="t2", pattern_key="P", posture="DIAGNOSED")

    rows = await store.list_cases_for_pattern(tenant_id="t1", pattern_key="P")
    assert [r["case_id"] for r in rows] == ["p2", "p1", "p0"]


async def test_list_cases_for_pattern_ton_trong_limit(store):
    for i in range(5):
        await store.open_case(
            case_id=f"c{i}", tenant_id="t1", pattern_key="P", posture="DIAGNOSED"
        )
    rows = await store.list_cases_for_pattern(tenant_id="t1", pattern_key="P", limit=2)
    assert len(rows) == 2


async def test_last_case_for_pattern_tra_ca_gan_nhat(store):
    """Nguồn cho câu 'đây là lần thứ N, tôi đã báo ngày X'."""
    await store.open_case(case_id="c1", tenant_id="t1", pattern_key="P", posture="DIAGNOSED")
    await store.open_case(case_id="c2", tenant_id="t1", pattern_key="P", posture="DIAGNOSED")
    last = await store.last_case_for_pattern(tenant_id="t1", pattern_key="P")
    assert last["case_id"] == "c2"
    assert last["occurrence_no"] == 2
    assert await store.last_case_for_pattern(tenant_id="t1", pattern_key="CHUA-GAP") is None


async def test_list_patterns_theo_tenant(store):
    await store.open_case(case_id="a", tenant_id="t1", pattern_key="P", posture="DIAGNOSED")
    await store.open_case(case_id="b", tenant_id="t1", pattern_key="Q", posture="REFUSED")
    await store.open_case(case_id="c", tenant_id="t2", pattern_key="R", posture="DIAGNOSED")
    assert await store.list_patterns(tenant_id="t1") == ["P", "Q"]


async def test_get_case_khong_ton_tai_tra_None(store):
    assert await store.get_case("khong-co") is None
