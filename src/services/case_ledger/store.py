"""Truy cập sổ ca (Postgres ``omni_admin``).

Tách khỏi ``AdminConfigRepo`` có chủ đích: repo đó đã >1200 dòng và lo cấu hình
tenant; sổ ca là bằng chứng năng lực, vòng đời khác hẳn. Gộp vào chỉ làm một file
lớn thêm mà không được gì.

Bất biến quan trọng nằm ở **trigger DB** (`0012_case_ledger.sql`), không phải ở
module này — nếu ai đó viết một đường ghi khác bỏ qua file này, trigger vẫn chặn.
Các hàm dưới đây chỉ là đường đi thuận tiện, không phải hàng rào.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

VALID_POSTURES = ("DIAGNOSED", "REFUSED", "OUT_OF_SCOPE")
VALID_SOURCES = ("telegram", "hitl", "portal", "world")
VALID_DIAGNOSIS = ("UNJUDGED", "CORRECT", "INCORRECT", "PARTIAL")
VALID_REMEDY = ("UNJUDGED", "CORRECT", "INCORRECT", "PARTIAL", "NOT_APPLICABLE")


class CaseLedgerStore:
    """Đọc/ghi sổ ca. ``pool`` là asyncpg pool."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        # Nhớ MỘT LẦN rằng DB chưa có cột của migration 0014, để không ném/bắt
        # exception ở mọi lượt đọc trên một DB chưa di trú.
        self._domain_column_missing = False

    async def open_case(
        self,
        *,
        case_id: str,
        tenant_id: str,
        pattern_key: str,
        posture: str,
        lane: str = "",
        alertname: str = "",
        crat_ref: str | None = None,
    ) -> dict[str, Any]:
        """Mở ca NGAY LÚC Omni phát biểu — trước khi biết đúng sai.

        Đây là toàn bộ điểm mấu chốt của sổ ca. Mở ca sau khi biết kết quả cho phép
        loại bỏ những ca xấu khỏi thống kê; mở trước thì mẫu số đã chốt.

        Idempotent theo ``case_id``: gọi lại KHÔNG ghi đè gì (``DO NOTHING``), vì
        ghi đè sẽ là một đường vòng để đổi ``pattern_key`` mà không đụng UPDATE.

        ``occurrence_no``/``prior_case_id`` tính trong cùng câu lệnh từ lịch sử
        cùng ``pattern_key`` — trí nhớ không phụ thuộc caller nhớ truyền vào.
        """
        if posture not in VALID_POSTURES:
            raise ValueError(f"posture khong hop le: {posture!r}")
        if not case_id or not pattern_key or not tenant_id:
            raise ValueError("case_id/pattern_key/tenant_id la bat buoc")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Khoá theo (tenant, pattern) trong phạm vi transaction. Không có nó,
                # hai ca cùng pattern mở đồng thời — chuyện thường xuyên khi alert dồn
                # dập — đều đọc cùng một "ca gần nhất" ở READ COMMITTED rồi cùng cộng 1.
                # Hệ quả là "đây là lần thứ N" đếm sai và chuỗi prior_case_id rẽ nhánh:
                # trí nhớ hỏng âm thầm, không có lỗi nào bật ra.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))", f"{tenant_id}:{pattern_key}"
                )
                # CỐ Ý tra một khoá duy nhất ở đây, khác với đường ĐỌC bên dưới:
                # occurrence_no bị canh bởi ux_case_ledger_occurrence(tenant,
                # pattern_key, occurrence_no). Nếu bộ đếm lấy từ một khoá khác thì
                # nó có thể trả về số đã dùng cho khoá này ⇒ INSERT vỡ ràng buộc.
                # Trí nhớ liên-taxonomy do đường đọc (`last_case_for_pattern`) lo.
                prior = await conn.fetchrow(
                    "SELECT case_id, occurrence_no FROM omni_admin.case_ledger "
                    "WHERE tenant_id=$1 AND pattern_key=$2 "
                    "ORDER BY opened_at DESC LIMIT 1",
                    tenant_id,
                    pattern_key,
                )
                occurrence_no = int(prior["occurrence_no"]) + 1 if prior else 1
                prior_case_id = prior["case_id"] if prior else None

                row = await conn.fetchrow(
                    "INSERT INTO omni_admin.case_ledger "
                    "(case_id, tenant_id, pattern_key, lane, alertname, posture, "
                    " occurrence_no, prior_case_id, crat_ref) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
                    "ON CONFLICT (case_id) DO NOTHING "
                    "RETURNING *",
                    case_id, tenant_id, pattern_key, lane, alertname, posture,
                    occurrence_no, prior_case_id, crat_ref,
                )
                if row is None:  # đã tồn tại — trả hàng cũ, KHÔNG sửa
                    row = await conn.fetchrow(
                        "SELECT * FROM omni_admin.case_ledger WHERE case_id=$1", case_id
                    )
                    return dict(row) if row else {}

                # Nhãn từ THẾ GIỚI, thu được miễn phí: cùng một pattern xuất hiện
                # lần nữa nghĩa là ca trước chưa thật sự được xử lý dứt điểm. Đây là
                # tín hiệu mạnh nhất trong hệ thống vì Omni không bịa được — nó suy
                # ra từ việc sự cố có quay lại hay không, không phải từ lời khai của
                # chính nó. Cũng chính là thang đo "xử lý triệt để" trong mục tiêu.
                if prior_case_id:
                    await conn.execute(
                        "UPDATE omni_admin.case_ledger "
                        "SET recurred = TRUE, recurred_at = now() "
                        "WHERE case_id = $1 AND recurred = FALSE",
                        prior_case_id,
                    )
                return dict(row)

    async def record_verdict(
        self,
        *,
        case_id: str,
        source: str,
        actor: str = "",
        diagnosis: str | None = None,
        remedy: str | None = None,
        crat_ref: str | None = None,
    ) -> dict[str, Any]:
        """Ghi phán quyết của NGƯỜI (hoặc của thế giới), không bao giờ của Omni.

        ``source`` bị giới hạn ở tầng CHECK constraint — không có 'self'/'system'.
        Người làm không được là người chấm; đó là tinh thần SOX §404 mà CRAT của hệ
        thống này vốn đã xây theo.
        """
        if source not in VALID_SOURCES:
            raise ValueError(f"verdict_source khong hop le: {source!r}")
        if diagnosis is None and remedy is None:
            raise ValueError("phai co it nhat mot phan quyet")
        # Validate ngay tại đây thay vì phó mặc CHECK constraint: sai chính tả sẽ ném
        # asyncpg exception thô ở tận tầng DB, khó lần ra chỗ gọi. Cũng để đối xứng với
        # `posture` và `source` vốn đã validate tại chỗ.
        if diagnosis is not None and diagnosis not in VALID_DIAGNOSIS:
            raise ValueError(f"diagnosis_verdict khong hop le: {diagnosis!r}")
        if remedy is not None and remedy not in VALID_REMEDY:
            raise ValueError(f"remedy_verdict khong hop le: {remedy!r}")

        # Nguồn/actor ghi ĐÚNG nhãn được chấm lần này. Dùng chung một cột cho cả hai
        # nhãn thì lần ghi sau xoá dấu vết lần trước — mà hai nhãn này thường do hai
        # bên khác nhau chấm ở hai thời điểm khác nhau (người duyệt HITL chấm chẩn
        # đoán; chỉ THẾ GIỚI mới chấm được khắc phục, qua việc sự cố có tái diễn không).
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE omni_admin.case_ledger SET "
                "diagnosis_verdict = COALESCE($2, diagnosis_verdict), "
                "remedy_verdict    = COALESCE($3, remedy_verdict), "
                "diagnosis_source = CASE WHEN $2::text IS NULL THEN diagnosis_source ELSE $4 END, "
                "diagnosis_actor  = CASE WHEN $2::text IS NULL THEN diagnosis_actor  ELSE $5 END, "
                "diagnosis_at     = CASE WHEN $2::text IS NULL THEN diagnosis_at     ELSE now() END, "
                "remedy_source    = CASE WHEN $3::text IS NULL THEN remedy_source    ELSE $4 END, "
                "remedy_actor     = CASE WHEN $3::text IS NULL THEN remedy_actor     ELSE $5 END, "
                "remedy_at        = CASE WHEN $3::text IS NULL THEN remedy_at        ELSE now() END, "
                "crat_ref = COALESCE($6, crat_ref) "
                "WHERE case_id = $1 RETURNING *",
                case_id, diagnosis, remedy, source, actor, crat_ref,
            )
            return dict(row) if row else {}

    async def mark_recurred(self, *, case_id: str) -> bool:
        """Sự thật từ thế giới: sự cố đã tái diễn.

        Nhãn mạnh nhất trong toàn hệ thống vì Omni không bịa được — nó đo từ hệ
        thống khách, không phải từ lời khai của chính nó.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE omni_admin.case_ledger SET recurred = TRUE, recurred_at = now() "
                "WHERE case_id = $1 AND recurred = FALSE RETURNING case_id",
                case_id,
            )
            return row is not None

    async def get_case(self, case_id: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM omni_admin.case_ledger WHERE case_id=$1", case_id
            )
            return dict(row) if row else None

    async def list_cases_for_pattern(
        self, *, tenant_id: str, pattern_key: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Mọi ca của một pattern — tra CẢ khoá đóng băng VÀ khoá suy từ domain.

        Cửa sổ chuyển tiếp lane→domain (migration 0014). ``pattern_key`` của sổ ca
        KHÔNG bị nắn (trigger 0012 cấm, và nắn nó là đường bùa số), nên ca lịch sử
        vẫn nằm dưới khoá cũ trong khi đường gọi mới hỏi bằng khoá domain. Tra một
        khoá thôi thì hồ sơ năng lực rỗng đi một cách âm thầm — và hồ sơ rỗng nghĩa
        là Omni mất mọi bằng chứng để giữ quyền đã được cấp.
        """
        async with self._pool.acquire() as conn:
            rows = await self._fetch_dual(
                conn,
                "SELECT * FROM omni_admin.case_ledger "
                "WHERE tenant_id=$1 AND ({match}) "
                "ORDER BY opened_at DESC LIMIT $3",
                tenant_id, pattern_key, limit,
            )
            return [dict(r) for r in rows]

    async def _fetch_dual(self, conn: Any, sql_tpl: str, *args: Any) -> list[Any]:
        """Chạy ``sql_tpl`` với vế khớp hai khoá; lùi về một khoá nếu DB chưa di trú.

        DB chưa apply 0014 (pod cũ, hoặc migration đang chạy dở) là chuyện triển
        khai — không phải lý do để đường đọc bằng chứng chết.
        """
        if not self._domain_column_missing:
            try:
                return list(await conn.fetch(
                    sql_tpl.format(match="pattern_key=$2 OR pattern_key_domain=$2"), *args
                ))
            except Exception as exc:  # noqa: BLE001
                if "pattern_key_domain" not in str(exc):
                    raise
                self._domain_column_missing = True
                logger.warning(
                    "case_ledger: chua co cot pattern_key_domain (migration 0014 chua "
                    "apply) — tra mot khoa"
                )
        return list(await conn.fetch(sql_tpl.format(match="pattern_key=$2"), *args))

    async def list_patterns(self, *, tenant_id: str) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT pattern_key FROM omni_admin.case_ledger "
                "WHERE tenant_id=$1",
                tenant_id,
            )
            return [str(r["pattern_key"]) for r in rows]

    async def last_case_for_pattern(
        self, *, tenant_id: str, pattern_key: str
    ) -> dict[str, Any] | None:
        """Ca gần nhất cùng pattern — nguồn cho câu 'đây là lần thứ N, tôi đã báo…'.

        Cũng tra hai khoá (xem ``list_cases_for_pattern``): nếu không, ngay sau khi
        đổi taxonomy Omni sẽ nói "đây là lần đầu" về một sự cố nó đã báo năm lần.
        """
        async with self._pool.acquire() as conn:
            rows = await self._fetch_dual(
                conn,
                "SELECT * FROM omni_admin.case_ledger "
                "WHERE tenant_id=$1 AND ({match}) "
                "ORDER BY opened_at DESC LIMIT 1",
                tenant_id, pattern_key,
            )
            return dict(rows[0]) if rows else None

    async def verdict_history(self, case_id: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM omni_admin.case_verdict_history "
                "WHERE case_id=$1 ORDER BY created_at ASC",
                case_id,
            )
            return [dict(r) for r in rows]
