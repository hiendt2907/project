-- migrations/omni_admin/0013_graduation_track_and_domain_check.sql
--
-- Tách khái niệm bị lẫn trong `playbook_graduation.domain` và bù lại constraint bị
-- thiếu. Xem `src/pkg/domain/taxonomy.py` (canonical) và
-- `plans/unify-domain-and-diagnostic-catalog-2026-07-30.md` (mục A).
--
-- HIỆN TRẠNG ĐO ĐƯỢC (runtime 2026-07-29/30):
--   Redis  omni:playbook:grad:default:k8s:PB-K8S-CPU-RESTART   ← playbook_governor
--   PG     playbook_graduation.domain = 'advisory'  (3 hàng)   ← advisory_promoter
-- `advisory` KHÔNG phải domain kỹ thuật — nó là NGUỒN HỌC (track). Hai writer không
-- biết nhau, và 0002_playbook.sql đặt CHECK(domain IN ...) trên bảng `playbook` nhưng
-- bỏ trống trên `playbook_graduation` — đúng chỗ cần nhất.
--
-- Hệ quả thật: `list_playbook_graduations()` (tier_loops + capacity_loops đọc để đề
-- xuất NÂNG TIER) trả hỗn hợp hai loại bản ghi khác bản chất. Con số dùng để trao
-- quyền tự chủ đang đếm gộp.
--
-- THỨ TỰ BẮT BUỘC: thêm cột → backfill/chuẩn hoá → gộp trùng → đổi PK → RỒI MỚI thêm
-- CHECK. Thêm CHECK trước backfill làm migration vỡ ngay ở hàng `advisory` đang tồn tại.
--
-- Idempotent: chạy lại nhiều lần không đổi kết quả (worker gọi run_migrations mỗi lần
-- khởi động, chạy nguyên file trong 1 transaction).

-- ---------------------------------------------------------------------------
-- 1. Cột `track` — nguồn học, tách khỏi domain kỹ thuật
-- ---------------------------------------------------------------------------
-- DEFAULT 'playbook': mọi hàng cũ không phải `advisory` đều đến từ playbook_governor,
-- tức là học từ playbook chạy có verify. Đặt default ở đây để backfill bên dưới chỉ
-- cần xử lý phần ngoại lệ.
ALTER TABLE omni_admin.playbook_graduation
    ADD COLUMN IF NOT EXISTS track TEXT NOT NULL DEFAULT 'playbook';

-- History cũng cần `track`: nếu không, lịch sử của hai track sẽ trộn vào nhau khi đối
-- chiếu CRAT, và không thể trả lời "quy trình này tốt nghiệp theo đường nào".
ALTER TABLE omni_admin.playbook_graduation_history
    ADD COLUMN IF NOT EXISTS track TEXT NOT NULL DEFAULT 'playbook';

-- ---------------------------------------------------------------------------
-- 1b. Gỡ PK CŨ TRƯỚC khi backfill
-- ---------------------------------------------------------------------------
-- Bắt buộc phải gỡ trước, không phải sau: PK cũ là (tenant_id, domain, playbook_id).
-- Backfill ở bước 2 đưa mọi hàng `advisory` về domain='unknown'; nếu tenant đó đã có
-- một hàng (tenant,'unknown',PB) của track playbook thì UPDATE sẽ vỡ vì trùng PK cũ —
-- trong khi dưới PK mới hai hàng đó hoàn toàn hợp lệ. Cùng lý do với alias
-- 'k8s'→'kubernetes' ở bước 3.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'omni_admin.playbook_graduation'::regclass
           AND conname  = 'playbook_graduation_pkey'
    ) THEN
        ALTER TABLE omni_admin.playbook_graduation
            DROP CONSTRAINT playbook_graduation_pkey;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Backfill — khớp `split_legacy_graduation_domain()` trong taxonomy.py
-- ---------------------------------------------------------------------------
-- Giá trị nằm trong ALL_TRACKS ⇒ đó là track, và domain THẬT SỰ chưa biết ⇒ 'unknown'.
-- Cố ý KHÔNG đoán domain từ playbook_id: đoán sai domain rồi dùng để cấp quyền còn tệ
-- hơn thừa nhận là chưa biết.
UPDATE omni_admin.playbook_graduation
SET track = lower(btrim(domain)), domain = 'unknown'
WHERE lower(btrim(domain)) IN ('advisory', 'playbook', 'execution');

UPDATE omni_admin.playbook_graduation_history
SET track = lower(btrim(domain)), domain = 'unknown'
WHERE lower(btrim(domain)) IN ('advisory', 'playbook', 'execution');

-- ---------------------------------------------------------------------------
-- 3. Chuẩn hoá `domain` còn lại về canonical
-- ---------------------------------------------------------------------------
-- Bảng alias phải KHỚP `_ALIASES` trong src/pkg/domain/taxonomy.py. Thà ánh xạ về
-- 'unknown' còn hơn giữ một tên lạ: cái tên lạ sẽ chặn CHECK ở bước 5 và làm cả
-- migration vỡ lúc worker khởi động.
CREATE TEMP TABLE _domain_alias (legacy TEXT PRIMARY KEY, canonical TEXT NOT NULL)
    ON COMMIT DROP;
INSERT INTO _domain_alias (legacy, canonical) VALUES
    ('kubernetes','kubernetes'), ('k8s','kubernetes'), ('k8s_cluster','kubernetes'),
    ('container','kubernetes'), ('container_logs','kubernetes'), ('docker','kubernetes'),
    ('os_host','os_host'), ('os','os_host'), ('os_system','os_host'), ('linux','os_host'),
    ('host','os_host'), ('vm','os_host'), ('baremetal','os_host'), ('os_baremetal','os_host'),
    ('network','network'), ('net','network'),
    ('storage','storage'), ('disk','storage'), ('filesystem','storage'), ('fs','storage'),
    ('database','database'), ('db','database'), ('sql','database'),
    ('service','service'), ('services','service'), ('systemd','service'),
    ('application','application'), ('app','application'), ('api','application'),
    ('http','application'),
    ('security','security'), ('siem','security'), ('sec','security'),
    ('hardware','hardware'), ('hw','hardware'),
    ('unknown','unknown');

UPDATE omni_admin.playbook_graduation g
SET domain = COALESCE(
        (SELECT a.canonical FROM _domain_alias a
          WHERE a.legacy = replace(lower(btrim(g.domain)), '-', '_')),
        'unknown')
WHERE domain <> COALESCE(
        (SELECT a.canonical FROM _domain_alias a
          WHERE a.legacy = replace(lower(btrim(g.domain)), '-', '_')),
        'unknown');

UPDATE omni_admin.playbook_graduation_history h
SET domain = COALESCE(
        (SELECT a.canonical FROM _domain_alias a
          WHERE a.legacy = replace(lower(btrim(h.domain)), '-', '_')),
        'unknown')
WHERE domain <> COALESCE(
        (SELECT a.canonical FROM _domain_alias a
          WHERE a.legacy = replace(lower(btrim(h.domain)), '-', '_')),
        'unknown');

-- ---------------------------------------------------------------------------
-- 4. PK: (tenant_id, domain, playbook_id) → (tenant_id, track, domain, playbook_id)
-- ---------------------------------------------------------------------------
-- LỰA CHỌN: thêm `track` vào PK, KHÔNG gộp hai track vào một hàng.
-- Lý do: một pattern học từ phán quyết người (track=advisory, auto_execute LUÔN false)
-- và cùng playbook_id đó học từ mutation có verify (track=playbook/execution) là HAI
-- bằng chứng khác bản chất. Gộp counter của chúng lại chính là cái bug đang phải sửa —
-- nó lại tạo ra một con số đếm gộp, chỉ là ở tầng hàng thay vì tầng truy vấn.
--
-- Nhưng chuẩn hoá alias ở bước 3 CÓ THỂ làm hai hàng trùng PK mới, ví dụ
-- (default,'k8s','PB-1') và (default,'kubernetes','PB-1') cùng thành 'kubernetes'.
-- Hai hàng đó là CÙNG một bằng chứng viết dưới hai cái tên, nên ở đây gộp counter là
-- đúng: cộng success/fail, giữ state của hàng được cập nhật gần nhất (state là hệ quả
-- của counter, và hàng mới nhất phản ánh quyết định gần nhất của governor).
-- Gộp phải làm bằng 3 câu lệnh tách rời (temp table → DELETE → INSERT), KHÔNG phải một
-- câu WITH data-modifying: trong cùng một câu lệnh, DELETE và INSERT thấy cùng một
-- snapshot, nên hàng sắp bị xoá vẫn hiện diện với unique index lúc INSERT và gây
-- unique_violation. Tách ra thì thứ tự là tường minh.
CREATE TEMP TABLE _grad_merged ON COMMIT DROP AS
SELECT g.tenant_id, g.track, g.domain, g.playbook_id,
       s.success_count, s.fail_count, s.updated_at,
       g.state, g.last_outcome, g.updated_by, g.crat_ref
  FROM (
        SELECT tenant_id, track, domain, playbook_id,
               sum(success_count)::int AS success_count,
               sum(fail_count)::int    AS fail_count,
               max(updated_at)         AS updated_at
          FROM omni_admin.playbook_graduation
         GROUP BY tenant_id, track, domain, playbook_id
        HAVING count(*) > 1
       ) s
  -- Hàng "thắng" = hàng được cập nhật gần nhất: `state` là hệ quả của counter, và
  -- quyết định gần nhất của governor là quyết định còn hiệu lực.
  JOIN LATERAL (
        SELECT g2.state, g2.last_outcome, g2.updated_by, g2.crat_ref,
               g2.tenant_id, g2.track, g2.domain, g2.playbook_id
          FROM omni_admin.playbook_graduation g2
         WHERE g2.tenant_id = s.tenant_id AND g2.track = s.track
           AND g2.domain = s.domain AND g2.playbook_id = s.playbook_id
         ORDER BY g2.updated_at DESC
         LIMIT 1
       ) g ON TRUE;

DELETE FROM omni_admin.playbook_graduation g
 USING _grad_merged m
 WHERE g.tenant_id = m.tenant_id AND g.track = m.track
   AND g.domain = m.domain AND g.playbook_id = m.playbook_id;

INSERT INTO omni_admin.playbook_graduation
    (tenant_id, domain, playbook_id, state, success_count, fail_count,
     last_outcome, updated_by, updated_at, crat_ref, track)
SELECT tenant_id, domain, playbook_id, state, success_count, fail_count,
       last_outcome, updated_by, updated_at, crat_ref, track
  FROM _grad_merged;

DO $$
BEGIN
    -- PK cũ đã gỡ ở bước 1b; dựng lại theo hình dạng mới. Chạy lại = no-op.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'omni_admin.playbook_graduation'::regclass
           AND conname  = 'playbook_graduation_pkey'
    ) THEN
        ALTER TABLE omni_admin.playbook_graduation
            ADD CONSTRAINT playbook_graduation_pkey
            PRIMARY KEY (tenant_id, track, domain, playbook_id);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 5. CHECK — cái đang thiếu. Chỉ đặt SAU khi dữ liệu đã sạch.
-- ---------------------------------------------------------------------------
-- Giá trị PHẢI khớp ALL_DOMAINS / ALL_TRACKS trong src/pkg/domain/taxonomy.py.
-- `unknown` được phép ở đây (khác bảng `playbook`) vì backfill hàng advisory buộc phải
-- ghi 'unknown' — chặn nó là chặn chính dữ liệu lịch sử hợp lệ.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'omni_admin.playbook_graduation'::regclass
           AND conname = 'ck_pb_grad_domain'
    ) THEN
        ALTER TABLE omni_admin.playbook_graduation
            ADD CONSTRAINT ck_pb_grad_domain CHECK (domain IN (
                'kubernetes','os_host','network','storage','database',
                'service','application','security','hardware','unknown'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'omni_admin.playbook_graduation'::regclass
           AND conname = 'ck_pb_grad_track'
    ) THEN
        ALTER TABLE omni_admin.playbook_graduation
            ADD CONSTRAINT ck_pb_grad_track
            CHECK (track IN ('advisory','playbook','execution'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'omni_admin.playbook_graduation_history'::regclass
           AND conname = 'ck_pb_grad_hist_track'
    ) THEN
        ALTER TABLE omni_admin.playbook_graduation_history
            ADD CONSTRAINT ck_pb_grad_hist_track
            CHECK (track IN ('advisory','playbook','execution'));
    END IF;
END $$;

-- Truy vấn nóng của tier_loops/capacity_loops là "graduation theo tenant + track".
CREATE INDEX IF NOT EXISTS ix_pb_grad_tenant_track
    ON omni_admin.playbook_graduation(tenant_id, track, state);

-- ---------------------------------------------------------------------------
-- 6. Bảng `playbook`: CHECK cũ liệt kê từ vựng cũ ('k8s','os','api',...)
-- ---------------------------------------------------------------------------
-- `PlaybookDomain` nay dẫn xuất từ CANONICAL_DOMAINS, nên writer tương lai sẽ ghi
-- 'kubernetes'/'os_host'/'application'. Giữ CHECK cũ là dựng một cái bẫy: code hợp lệ
-- bị DB từ chối. Chuẩn hoá dữ liệu rồi thay CHECK.
UPDATE omni_admin.playbook p
SET domain = COALESCE(
        (SELECT a.canonical FROM _domain_alias a
          WHERE a.legacy = replace(lower(btrim(p.domain)), '-', '_')),
        'unknown')
WHERE domain <> COALESCE(
        (SELECT a.canonical FROM _domain_alias a
          WHERE a.legacy = replace(lower(btrim(p.domain)), '-', '_')),
        'unknown');

DO $$
DECLARE
    old_check TEXT;
BEGIN
    SELECT conname INTO old_check FROM pg_constraint
     WHERE conrelid = 'omni_admin.playbook'::regclass
       AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%domain%'
       AND pg_get_constraintdef(oid) LIKE '%''k8s''%';
    IF old_check IS NOT NULL THEN
        EXECUTE format('ALTER TABLE omni_admin.playbook DROP CONSTRAINT %I', old_check);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'omni_admin.playbook'::regclass
           AND conname = 'ck_playbook_domain'
    ) THEN
        -- Bảng catalogue: KHÔNG cho 'unknown'. Một playbook không biết chạy trên domain
        -- nào thì không thể chọn được, khác với bằng chứng lịch sử ở bảng graduation.
        ALTER TABLE omni_admin.playbook
            ADD CONSTRAINT ck_playbook_domain CHECK (domain IN (
                'kubernetes','os_host','network','storage','database',
                'service','application','security','hardware'));
    END IF;
END $$;
