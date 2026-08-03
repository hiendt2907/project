"""Cửa sổ chuyển tiếp lane→domain: đường TRA phải khớp CẢ HAI khoá.

Vì sao có file test riêng: đây là chỗ hỏng ÂM THẦM duy nhất của Phase 3. Nếu đường
tra chỉ khớp khoá mới thì mọi ``scope_grant`` khách đã duyệt ngừng khớp — không
exception, không log, Omni chỉ đơn giản mất quyền và quay lại xin. Không có test nào
khác trong dự án làm đỏ được kịch bản đó, vì mọi test cũ đều dùng CÙNG một khoá cho
cả ghi và đọc.

Fake asyncpg viết tay (không AsyncMock): tính chất cần chứng minh là tính chất của
vế WHERE. Một mock trả sẵn sẽ xanh kể cả khi vế thứ hai bị bỏ mất.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from pkg.domain.taxonomy import LANE_TO_DOMAIN
from services.case_ledger.store import CaseLedgerStore
from services.case_ledger.store_scope import ScopeStore

MIGRATION = Path(__file__).resolve().parents[1] / "migrations/omni_admin/0014_lane_to_domain.sql"


# ── Fake asyncpg tối thiểu, bám theo vế WHERE thật ───────────────────────────


class FakeConn:
    def __init__(self, owner: "FakeDB") -> None:
        self.db = owner

    async def fetch(self, sql: str, *args):
        s = " ".join(sql.split())

        if "FROM omni_admin.case_ledger" in s:
            dual = "pattern_key_domain=$2" in s
            if dual and not self.db.has_0014:
                raise RuntimeError(
                    'column "pattern_key_domain" does not exist'
                )
            tenant, key = args[0], args[1]
            limit = args[2] if len(args) > 2 else 1
            hits = [
                r for r in self.db.cases
                if r["tenant_id"] == tenant and (
                    r["pattern_key"] == key
                    or (dual and r.get("pattern_key_domain") == key)
                )
            ]
            hits.sort(key=lambda r: r["opened_at"], reverse=True)
            return hits[:limit]

        if "FROM omni_admin.scope_grant" in s:
            dual = "pattern_key_legacy=$2" in s
            if dual and not self.db.has_0014:
                raise RuntimeError('column "pattern_key_legacy" does not exist')
            tenant, key = args[0], args[1]
            hits = [
                g for g in self.db.grants
                if g["tenant_id"] == tenant and (
                    g["pattern_key"] == key
                    or (dual and g.get("pattern_key_legacy") == key)
                )
            ]
            # ORDER BY (pattern_key=$2) DESC — khớp khoá mới thắng khoá lịch sử.
            hits.sort(key=lambda g: g["pattern_key"] == key, reverse=True)
            return hits[:1]

        raise AssertionError(f"fake khong hieu SQL: {s[:120]}")

    async def fetchrow(self, sql: str, *args):
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None


class FakeDB:
    def __init__(self, *, has_0014: bool = True) -> None:
        self.cases: list[dict] = []
        self.grants: list[dict] = []
        self.has_0014 = has_0014

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self)

    def add_case(self, *, tenant, pattern_key, domain_key="", opened_at=0):
        self.cases.append({
            "tenant_id": tenant,
            "pattern_key": pattern_key,
            "pattern_key_domain": domain_key,
            "case_id": f"c{len(self.cases)}",
            "opened_at": opened_at or len(self.cases),
            "posture": "DIAGNOSED",
            "diagnosis_verdict": "CORRECT",
            "recurred": False,
        })

    def add_grant(self, *, tenant, pattern_key, legacy="", scope="HITL_REQUIRED"):
        self.grants.append({
            "tenant_id": tenant,
            "pattern_key": pattern_key,
            "pattern_key_legacy": legacy,
            "granted_scope": scope,
            "frozen": False,
        })


# ── case_ledger: ca lịch sử vẫn phải tìm được bằng khoá domain ────────────────


async def test_list_cases_tim_duoc_ca_luu_duoi_khoa_cu_khi_hoi_bang_khoa_domain():
    """Hồ sơ năng lực không được rỗng đi sau khi đổi taxonomy.

    ``case_ledger.pattern_key`` KHÔNG bị nắn (trigger 0012 cấm), nên ca lịch sử nằm
    dưới khoá cũ trong khi đường gọi mới hỏi bằng khoá domain. Rỗng hồ sơ nghĩa là
    Omni mất mọi bằng chứng để giữ quyền đã được cấp.
    """
    db = FakeDB()
    db.add_case(tenant="acme", pattern_key="OLD-HASH", domain_key="NEW-HASH")
    store = CaseLedgerStore(db)

    rows = await store.list_cases_for_pattern(tenant_id="acme", pattern_key="NEW-HASH")
    assert len(rows) == 1
    assert rows[0]["pattern_key"] == "OLD-HASH"


async def test_list_cases_khong_tron_ca_cua_pattern_khac():
    """Khớp hai khoá KHÔNG được nới thành khớp bừa — lọt ca lạ vào là bùa số."""
    db = FakeDB()
    db.add_case(tenant="acme", pattern_key="OLD-A", domain_key="NEW-A")
    db.add_case(tenant="acme", pattern_key="OLD-B", domain_key="NEW-B")
    rows = await CaseLedgerStore(db).list_cases_for_pattern(
        tenant_id="acme", pattern_key="NEW-A"
    )
    assert [r["pattern_key"] for r in rows] == ["OLD-A"]


async def test_last_case_van_nho_lan_truoc_qua_khoa_domain():
    """Không có nhánh này, Omni nói 'đây là lần đầu' về sự cố nó đã báo nhiều lần."""
    db = FakeDB()
    db.add_case(tenant="acme", pattern_key="OLD", domain_key="NEW", opened_at=1)
    db.add_case(tenant="acme", pattern_key="OLD", domain_key="NEW", opened_at=2)
    last = await CaseLedgerStore(db).last_case_for_pattern(
        tenant_id="acme", pattern_key="NEW"
    )
    assert last is not None and last["opened_at"] == 2


async def test_case_ledger_lui_ve_mot_khoa_khi_db_chua_apply_0014(caplog):
    """DB chưa di trú là chuyện triển khai, không phải lý do làm chết đường đọc."""
    db = FakeDB(has_0014=False)
    db.add_case(tenant="acme", pattern_key="OLD", domain_key="NEW")
    store = CaseLedgerStore(db)

    assert await store.list_cases_for_pattern(tenant_id="acme", pattern_key="NEW") == []
    rows = await store.list_cases_for_pattern(tenant_id="acme", pattern_key="OLD")
    assert len(rows) == 1
    assert any("pattern_key_domain" in r.message for r in caplog.records)


async def test_case_ledger_chi_canh_bao_mot_lan_roi_nho_luon():
    """Không ném/bắt exception ở mọi lượt đọc trên một DB chưa di trú."""
    db = FakeDB(has_0014=False)
    store = CaseLedgerStore(db)
    await store.list_cases_for_pattern(tenant_id="acme", pattern_key="X")
    assert store._domain_column_missing is True


async def test_loi_khac_khong_bi_nuot():
    """Chỉ 'thiếu cột' được lùi. Mọi lỗi khác phải nổi lên — nuốt là mất tín hiệu."""
    db = FakeDB()

    class Boom(FakeConn):
        async def fetch(self, sql, *args):
            raise RuntimeError("connection reset by peer")

    @asynccontextmanager
    async def acquire():
        yield Boom(db)

    db.acquire = acquire  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="connection reset"):
        await CaseLedgerStore(db).list_cases_for_pattern(tenant_id="a", pattern_key="k")


# ── scope_grant: QUYỀN khách đã duyệt phải còn khớp ──────────────────────────


async def test_get_grant_tim_duoc_quyen_luu_duoi_khoa_lich_su():
    """Hàng chưa di trú được (không phục hồi được lane từ sổ ca) vẫn phải khớp."""
    db = FakeDB()
    db.add_grant(tenant="acme", pattern_key="OLD", legacy="OLD")
    grant = await ScopeStore(db).get_grant(tenant_id="acme", pattern_key="OLD")
    assert grant is not None and grant["granted_scope"] == "HITL_REQUIRED"


async def test_get_grant_khop_khoa_moi_sau_khi_da_viet_lai():
    """Hàng đã di trú: pattern_key = khoá domain, khoá cũ nằm ở pattern_key_legacy."""
    db = FakeDB()
    db.add_grant(tenant="acme", pattern_key="NEW", legacy="OLD")
    scope = ScopeStore(db)

    assert (await scope.get_grant(tenant_id="acme", pattern_key="NEW")) is not None
    # Đường gọi CHƯA nâng cấp (vẫn hỏi bằng khoá cũ) cũng không được mất quyền.
    assert (await scope.get_grant(tenant_id="acme", pattern_key="OLD")) is not None


async def test_get_grant_uu_tien_khoa_moi_khi_ca_hai_cung_tro_ve_mot_pattern():
    """Khớp CHÍNH XÁC ``pattern_key`` thắng khớp qua ``pattern_key_legacy``.

    Khoá lịch sử chỉ là lưới an toàn. Nếu để nó thắng, một grant cũ (bậc thấp hoặc
    bậc cao hơn) sẽ đè grant đang có hiệu lực — quyền hiệu lực phải xác định được,
    không phụ thuộc thứ tự hàng trong bảng.
    """
    db = FakeDB()
    db.add_grant(tenant="acme", pattern_key="KEY", legacy="OTHER", scope="SUGGEST_ONLY")
    db.add_grant(tenant="acme", pattern_key="MIGRATED", legacy="KEY", scope="AUTO_EXECUTE")
    grant = await ScopeStore(db).get_grant(tenant_id="acme", pattern_key="KEY")
    assert grant is not None and grant["granted_scope"] == "SUGGEST_ONLY"


async def test_get_grant_cach_ly_tenant():
    db = FakeDB()
    db.add_grant(tenant="globex", pattern_key="NEW", legacy="OLD")
    assert await ScopeStore(db).get_grant(tenant_id="acme", pattern_key="OLD") is None


async def test_get_grant_lui_ve_mot_khoa_khi_db_chua_apply_0014():
    db = FakeDB(has_0014=False)
    db.add_grant(tenant="acme", pattern_key="OLD", legacy="")
    scope = ScopeStore(db)
    assert await scope.get_grant(tenant_id="acme", pattern_key="OLD") is not None
    assert scope._legacy_column_missing is True
    # Lượt sau đi thẳng đường một khoá, không ném/bắt lại.
    assert await scope.get_grant(tenant_id="acme", pattern_key="OLD") is not None


# ── Migration 0014 không được lệch khỏi taxonomy Python ──────────────────────


def test_migration_0014_map_lane_giong_het_taxonomy_python():
    """Bản đồ lane→domain tồn tại HAI BẢN (SQL + Python). Lệch nhau là khoá mới do
    migration sinh không bao giờ khớp khoá Python sinh lúc chạy — đúng thứ hỏng âm
    thầm mà cả Phase 3 dựng lên để chặn."""
    sql = MIGRATION.read_text(encoding="utf-8")
    body = sql.split("CREATE OR REPLACE FUNCTION omni_admin.lane_to_domain", 1)[1]
    body = body.split("$$ LANGUAGE sql", 1)[0]
    pairs = dict(re.findall(r"WHEN '([a-z_]+)'\s+THEN '([a-z_]+)'", body))
    assert pairs == LANE_TO_DOMAIN


def test_migration_0014_giu_sys_hard_fail_va_onboarding_o_unknown():
    """Chống 'sửa cho đẹp': hai lane này CỐ Ý không map 1-1, domain thật phải lấy từ
    collector nào phát ra. Đoán sai domain rồi cấp quyền theo nó còn tệ hơn."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "WHEN 'sys_hard_fail'        THEN 'unknown'" in sql
    assert "WHEN 'onboarding_discovery' THEN 'unknown'" in sql


def test_migration_0014_khong_xoa_cot_lane_va_khong_nan_pattern_key_cua_so_ca():
    """Hai bất biến của Phase 3, dễ bị một lần 'dọn cho gọn' phá."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "DROP COLUMN" not in sql.upper()
    assert "UPDATE omni_admin.case_ledger\n   SET pattern_key =" not in sql
    assert "DISABLE TRIGGER" not in sql.upper()


def test_migration_0014_idempotent_moi_cau_alter():
    """`run_migrations()` apply lại MỌI file *.sql mỗi lần worker khởi động."""
    sql = MIGRATION.read_text(encoding="utf-8")
    adds = re.findall(r"ADD COLUMN(?! IF NOT EXISTS)", sql)
    assert adds == []
    creates = re.findall(r"CREATE (?:UNIQUE )?INDEX(?! IF NOT EXISTS)", sql)
    assert creates == []
    tables = re.findall(r"CREATE TABLE(?! IF NOT EXISTS)", sql)
    assert tables == []
    # Bước viết lại scope_grant chỉ được chạy MỘT lần.
    assert "FROM omni_admin.migration_0014_state" in sql
