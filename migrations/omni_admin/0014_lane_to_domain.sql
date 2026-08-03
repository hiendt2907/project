-- lane (trục A) → domain: DI TRÚ KHOÁ QUYỀN.
-- Kế hoạch + lý do: plans/lane-to-domain-and-omni-decides-2026-07-30.md §0b, Phase 3.
-- Thứ tự chạy / dump / rollback: docs/runbooks/lane-to-domain-migration.md
--
-- ── Rủi ro migration này tồn tại để chặn ────────────────────────────────────
-- `advisory_pattern_key()` (src/services/learning_promoter/advisory_promoter.py:48)
-- sinh khoá = sha256("lane|alertname")[:32]. Khoá đó là PRIMARY KEY của
-- `scope_grant` — quyền khách hàng đã DUYỆT cho Omni theo từng loại việc.
-- Đổi giá trị lane sang domain mà không di trú khoá thì mọi grant NGỪNG KHỚP,
-- không exception nào, không log nào: Omni chỉ đơn giản mất quyền và quay lại xin.
-- Đây là kiểu hỏng tệ nhất vì nó âm thầm.
--
-- ── Bất đối xứng có chủ đích: BẰNG CHỨNG không nắn, QUYỀN thì di trú ────────
-- `case_ledger.pattern_key` KHÔNG bị sửa ở đây. Hai lý do độc lập, cả hai đều đủ:
--   1. trigger `trg_case_ledger_guard` (0012) cấm UPDATE cột đó — nắn pattern_key
--      là "đường bùa số tinh vi nhất", và migration không phải ngoại lệ được miễn.
--   2. `ux_case_ledger_occurrence(tenant_id, pattern_key, occurrence_no)`: hai lane
--      khác nhau cùng map về `unknown` (SYS_HARD_FAIL và ONBOARDING_DISCOVERY) sẽ
--      đụng nhau ⇒ migration sẽ vỡ giữa đường trên dữ liệu thật.
-- Nên `case_ledger` chỉ THÊM cột mô tả: `domain`, `pattern_key_legacy` (bản chụp
-- khoá lịch sử), `pattern_key_domain` (khoá mới, để đường đọc tra được cả hai).
-- `scope_grant.pattern_key` thì ĐƯỢC viết lại — nó là cấu hình quyền, đổi liên tục
-- theo quyết định của người, không phải bằng chứng đóng băng.
--
-- Idempotent: file này chạy LẠI mỗi lần worker khởi động (`run_migrations()` apply
-- mọi *.sql, không có bảng version). Mọi ALTER dùng IF NOT EXISTS; bước viết lại
-- `scope_grant` chạy MỘT LẦN, canh bằng bảng mốc `migration_0014_state`.
--
-- Yêu cầu: PostgreSQL ≥ 11 (hàm `sha256()` dựng sẵn, không cần pgcrypto).

-- ── 1. Hai hàm thuần, khớp từng ký tự với Python ─────────────────────────────

-- Bản sao SQL của `pkg.domain.taxonomy.LANE_TO_DOMAIN`. `sys_hard_fail` và
-- `onboarding_discovery` → 'unknown' CỐ Ý, không đoán: sys_hard_fail đang gánh cả
-- database/storage/service/kubernetes (domain thật phải lấy từ COLLECTOR nào phát
-- ra), còn onboarding_discovery là một PHA của vòng đời, không phải lĩnh vực kỹ
-- thuật. Đoán sai domain rồi dùng nó để cấp quyền còn tệ hơn thừa nhận chưa biết.
CREATE OR REPLACE FUNCTION omni_admin.lane_to_domain(lane TEXT)
RETURNS TEXT AS $$
    SELECT CASE replace(lower(trim(COALESCE(lane, ''))), '-', '_')
        WHEN 'sys_resource'         THEN 'os_host'
        WHEN 'app_http'             THEN 'application'
        WHEN 'siem_security'        THEN 'security'
        WHEN 'sys_hard_fail'        THEN 'unknown'
        WHEN 'onboarding_discovery' THEN 'unknown'
        ELSE 'unknown'
    END;
$$ LANGUAGE sql IMMUTABLE;

-- Bản sao SQL của `advisory_pattern_key()`. Phải khớp TUYỆT ĐỐI, kể cả `lower()`
-- và cắt 32 ký tự: lệch một ký tự là khoá mới không bao giờ khớp khoá Python sinh
-- ra lúc chạy, và hậu quả lại đúng là thứ migration này đang chặn.
CREATE OR REPLACE FUNCTION omni_admin.advisory_pattern_key(part_a TEXT, part_b TEXT)
RETURNS TEXT AS $$
    SELECT substr(
        encode(
            sha256(convert_to(lower(COALESCE(part_a, '') || '|' || COALESCE(part_b, '')), 'UTF8')),
            'hex'
        ), 1, 32);
$$ LANGUAGE sql IMMUTABLE;

-- ── 2. case_ledger — chỉ thêm cột, không nắn pattern_key ─────────────────────

ALTER TABLE omni_admin.case_ledger
    ADD COLUMN IF NOT EXISTS domain             TEXT NOT NULL DEFAULT '';
ALTER TABLE omni_admin.case_ledger
    ADD COLUMN IF NOT EXISTS pattern_key_legacy TEXT NOT NULL DEFAULT '';
-- Khoá mới suy từ domain. Tách khỏi `pattern_key` (đóng băng) để đường đọc tra
-- được CẢ HAI trong cửa sổ chuyển tiếp mà không phá bất biến nào.
ALTER TABLE omni_admin.case_ledger
    ADD COLUMN IF NOT EXISTS pattern_key_domain TEXT NOT NULL DEFAULT '';

UPDATE omni_admin.case_ledger
   SET domain = omni_admin.lane_to_domain(lane)
 WHERE domain = '';

UPDATE omni_admin.case_ledger
   SET pattern_key_legacy = pattern_key
 WHERE pattern_key_legacy = '';

-- Dạng hash 32 hex = khoá do `advisory_pattern_key` sinh ⇒ tính lại từ domain.
UPDATE omni_admin.case_ledger
   SET pattern_key_domain = omni_admin.advisory_pattern_key(domain, alertname)
 WHERE pattern_key_domain = ''
   AND pattern_key ~ '^[0-9a-f]{32}$';

-- Dạng văn bản 'LANE:alertname:tool' = khoá do `pattern_key_for_hitl` sinh
-- (hitl_link.py). Chỉ thay token lane ở ĐẦU, giữ nguyên phần còn lại.
UPDATE omni_admin.case_ledger
   SET pattern_key_domain = domain || substr(pattern_key, length(lane) + 1)
 WHERE pattern_key_domain = ''
   AND lane <> ''
   AND pattern_key LIKE lane || ':%';

CREATE INDEX IF NOT EXISTS ix_case_ledger_pattern_domain
    ON omni_admin.case_ledger(tenant_id, pattern_key_domain, opened_at DESC);

-- ── 3. scope_grant — bảng KHÔNG có cột lane (đã kiểm 0012) ───────────────────
-- PK là (tenant_id, pattern_key); ngoài ra chỉ có granted_scope/granted_by/
-- granted_at/frozen/frozen_reason. Không có `lane`, nên KHÔNG thêm cột `domain`:
-- domain của một grant không suy được từ chính bảng đó, phải suy qua case_ledger.
-- Thêm một cột `domain` rỗng ở đây chỉ tạo một trường trông như nguồn sự thật mà
-- không ai ghi đúng.
ALTER TABLE omni_admin.scope_grant
    ADD COLUMN IF NOT EXISTS pattern_key_legacy TEXT NOT NULL DEFAULT '';

UPDATE omni_admin.scope_grant
   SET pattern_key_legacy = pattern_key
 WHERE pattern_key_legacy = '';

CREATE INDEX IF NOT EXISTS ix_scope_grant_legacy
    ON omni_admin.scope_grant(tenant_id, pattern_key_legacy);

-- Tương tự cho scope_request: đơn đang treo/đang cooldown phải còn khớp sau khi
-- đổi khoá, nếu không Omni sẽ xin lại một pattern vừa bị từ chối.
ALTER TABLE omni_admin.scope_request
    ADD COLUMN IF NOT EXISTS pattern_key_legacy TEXT NOT NULL DEFAULT '';

UPDATE omni_admin.scope_request
   SET pattern_key_legacy = pattern_key
 WHERE pattern_key_legacy = '';

-- ── 4. Ảnh chụp TRƯỚC khi viết lại + bước một lần ────────────────────────────
-- Ảnh chụp không phải để cho đẹp: nó là mẫu số của gate "số grant khớp được
-- TRƯỚC và SAU migration phải bằng nhau" (scripts/verify_case_ledger.sh, nhóm E)
-- và là nguồn duy nhất để rollback. Không có nó thì sau khi chạy, không ai chứng
-- minh được là không mất grant nào.
CREATE TABLE IF NOT EXISTS omni_admin.scope_grant_premigration_0014 (
    tenant_id     TEXT NOT NULL,
    pattern_key   TEXT NOT NULL,
    granted_scope TEXT NOT NULL,
    granted_by    TEXT NOT NULL DEFAULT '',
    frozen        BOOLEAN NOT NULL DEFAULT FALSE,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, pattern_key)
);

CREATE TABLE IF NOT EXISTS omni_admin.migration_0014_state (
    id          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    rewritten   INTEGER NOT NULL DEFAULT 0,
    kept_legacy INTEGER NOT NULL DEFAULT 0,  -- hàng giữ nguyên khoá cũ (tra qua legacy)
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $mig$
DECLARE
    n_rewritten INTEGER := 0;
    n_kept      INTEGER := 0;
BEGIN
    IF EXISTS (SELECT 1 FROM omni_admin.migration_0014_state) THEN
        RETURN;  -- đã chạy một lần; file này được apply lại mỗi lần worker khởi động
    END IF;

    INSERT INTO omni_admin.scope_grant_premigration_0014
        (tenant_id, pattern_key, granted_scope, granted_by, frozen)
    SELECT tenant_id, pattern_key, granted_scope, granted_by, frozen
      FROM omni_admin.scope_grant
    ON CONFLICT (tenant_id, pattern_key) DO NOTHING;

    -- Bản đồ khoá cũ → khoá mới, PHỤC HỒI TỪ case_ledger: khoá cũ là hash một
    -- chiều, không đảo được, nên nguồn duy nhất biết (lane, alertname) là sổ ca.
    -- `HAVING count(DISTINCT ...) = 1` loại mọi khoá cũ ánh xạ về NHIỀU khoá mới:
    -- ánh xạ nhập nhằng thì thà để nguyên và dựa vào đường tra legacy, còn hơn
    -- chọn bừa một nhánh rồi trao quyền theo nó.
    CREATE TEMP TABLE _pk_map ON COMMIT DROP AS
        SELECT tenant_id,
               pattern_key AS old_key,
               min(pattern_key_domain) AS new_key
          FROM omni_admin.case_ledger
         WHERE pattern_key_domain <> ''
           AND pattern_key_domain <> pattern_key
         GROUP BY tenant_id, pattern_key
        HAVING count(DISTINCT pattern_key_domain) = 1;

    -- Chỉ viết lại khi khoá mới CHƯA tồn tại cho tenant đó. Đụng khoá là hai lane
    -- gộp về cùng domain (SYS_HARD_FAIL + ONBOARDING_DISCOVERY → unknown): gộp hai
    -- grant thành một sẽ NÂNG quyền cho một loại việc chưa từng được duyệt. Bỏ
    -- qua, để đường tra legacy lo — mất khớp còn cứu được, cấp thừa quyền thì không.
    UPDATE omni_admin.scope_grant g
       SET pattern_key = m.new_key
      FROM _pk_map m
     WHERE g.tenant_id = m.tenant_id
       AND g.pattern_key = m.old_key
       AND NOT EXISTS (
           SELECT 1 FROM omni_admin.scope_grant x
            WHERE x.tenant_id = m.tenant_id AND x.pattern_key = m.new_key
       );
    GET DIAGNOSTICS n_rewritten = ROW_COUNT;

    SELECT count(*) INTO n_kept
      FROM omni_admin.scope_grant
     WHERE pattern_key = pattern_key_legacy;

    INSERT INTO omni_admin.migration_0014_state (id, rewritten, kept_legacy)
    VALUES (1, n_rewritten, n_kept)
    ON CONFLICT (id) DO NOTHING;

    RAISE NOTICE '0014: scope_grant viet lai=% giu nguyen khoa cu=%', n_rewritten, n_kept;
END
$mig$;

-- ── 5. KHÔNG xoá cột `lane` ở migration này ─────────────────────────────────
-- Cắt lane là Phase 4, chỉ sau khi fleet agent 3/3 gửi `domain` và không còn
-- đường đọc nào dùng lane trục A. Xoá sớm là mất luôn khả năng tính lại
-- `pattern_key_domain` nếu bản đồ domain phải sửa.
