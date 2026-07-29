-- Sổ ca — nguồn sự thật duy nhất để đánh giá năng lực Omni.
-- Thiết kế đầy đủ + lý do từng ràng buộc: plans/case-ledger-design-2026-07-30.md
--
-- Ca được MỞ lúc advisory phát ra (chưa biết đúng sai), không phải lúc có kết quả.
-- Đó là toàn bộ điểm mấu chốt: mẫu số chốt trước, nên không thể loại ca xấu về sau.

CREATE TABLE IF NOT EXISTS omni_admin.case_ledger (
    case_id           TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    -- ĐÓNG BĂNG. Nắn pattern_key là đường bùa số tinh vi nhất: tách ca sai thành
    -- nhóm riêng, gộp ca đúng lại, mọi nhóm đều đẹp. Trigger bên dưới cấm UPDATE.
    pattern_key       TEXT NOT NULL,
    lane              TEXT NOT NULL DEFAULT '',
    alertname         TEXT NOT NULL DEFAULT '',

    -- Omni đã làm gì với ca này. REFUSED cũng là một ca và cũng vào mẫu số —
    -- nếu không, chiến lược tối ưu là từ chối mọi ca khó để giữ hồ sơ 100%.
    posture           TEXT NOT NULL
                      CHECK (posture IN ('DIAGNOSED','REFUSED','OUT_OF_SCOPE')),

    -- Hai nhãn TÁCH RỜI: đoán trúng nguyên nhân, và làm theo có hết.
    -- Gộp một nhãn là mất vĩnh viễn thông tin nó yếu ở khâu nào.
    diagnosis_verdict TEXT NOT NULL DEFAULT 'UNJUDGED'
                      CHECK (diagnosis_verdict IN
                             ('UNJUDGED','CORRECT','INCORRECT','PARTIAL')),
    remedy_verdict    TEXT NOT NULL DEFAULT 'UNJUDGED'
                      CHECK (remedy_verdict IN
                             ('UNJUDGED','CORRECT','INCORRECT','PARTIAL','NOT_APPLICABLE')),

    -- Người chấm KHÔNG BAO GIỜ là Omni. Không có giá trị 'self'/'system'.
    --
    -- Nguồn/actor TÁCH THEO TỪNG NHÃN, không dùng chung một bộ. Hai nhãn thường được
    -- chấm bởi hai bên khác nhau ở hai thời điểm khác nhau: người duyệt HITL chấm
    -- `diagnosis` lúc hành động chưa chạy, còn `remedy` chỉ có thể do THẾ GIỚI chấm về
    -- sau (sự cố có tái diễn không). Dùng chung một `verdict_source` thì lần ghi sau đè
    -- lần trước và hàng ledger nói dối về việc ai đã chấm cái gì.
    diagnosis_source  TEXT
                      CHECK (diagnosis_source IS NULL OR
                             diagnosis_source IN ('telegram','hitl','portal','world')),
    diagnosis_actor   TEXT,
    diagnosis_at      TIMESTAMPTZ,
    remedy_source     TEXT
                      CHECK (remedy_source IS NULL OR
                             remedy_source IN ('telegram','hitl','portal','world')),
    remedy_actor      TEXT,
    remedy_at         TIMESTAMPTZ,

    -- Trí nhớ: lần thứ mấy của cùng pattern, và ca trước là ca nào.
    occurrence_no     INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_no >= 1),
    prior_case_id     TEXT,

    -- Sự thật từ thế giới — Omni không bịa được vì đo từ hệ thống khách.
    recurred          BOOLEAN NOT NULL DEFAULT FALSE,
    recurred_at       TIMESTAMPTZ,

    crat_ref          TEXT,
    opened_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_case_ledger_pattern
    ON omni_admin.case_ledger(tenant_id, pattern_key, opened_at DESC);
-- Chặn cứng hai ca cùng pattern nhận cùng occurrence_no. Ở READ COMMITTED (mặc định
-- Postgres) hai transaction đồng thời đều đọc được cùng một 'ca gần nhất' rồi cùng
-- cộng 1 — kịch bản xảy ra thường xuyên khi alert dồn dập. Không có ràng buộc này thì
-- "đây là lần thứ N" đếm sai và chuỗi prior_case_id rẽ nhánh, tức TRÍ NHỚ hỏng âm thầm.
CREATE UNIQUE INDEX IF NOT EXISTS ux_case_ledger_occurrence
    ON omni_admin.case_ledger(tenant_id, pattern_key, occurrence_no);
CREATE INDEX IF NOT EXISTS ix_case_ledger_unjudged
    ON omni_admin.case_ledger(tenant_id, opened_at DESC)
    WHERE diagnosis_verdict = 'UNJUDGED';

-- Append-only: mọi lần đổi verdict đều để lại vết, kể cả người đổi.
CREATE TABLE IF NOT EXISTS omni_admin.case_verdict_history (
    id            BIGSERIAL PRIMARY KEY,
    case_id       TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    field         TEXT NOT NULL CHECK (field IN ('diagnosis','remedy')),
    from_verdict  TEXT NOT NULL,
    to_verdict    TEXT NOT NULL,
    source        TEXT NOT NULL,
    actor         TEXT NOT NULL DEFAULT '',
    crat_ref      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_case_verdict_hist
    ON omni_admin.case_verdict_history(tenant_id, case_id, created_at DESC);

-- Đơn xin mở rộng quyền — Omni CHỦ ĐỘNG xin theo TỪNG pattern_key, không xin
-- nâng tier tổng: bằng chứng nó có là bằng chứng theo loại việc.
CREATE TABLE IF NOT EXISTS omni_admin.scope_request (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    pattern_key     TEXT NOT NULL,
    requested_scope TEXT NOT NULL,
    -- Số liệu tại thời điểm xin, đóng băng để khách đối chiếu lại với CRAT.
    evidence        JSONB NOT NULL DEFAULT '{}'::jsonb,
    state           TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (state IN ('PENDING','APPROVED','REJECTED','WITHDRAWN')),
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    decision_note   TEXT,
    -- Bị từ chối thì khoá xin lại: nếu xin miễn phí, chiến lược tối ưu là xin
    -- liên tục tới lúc admin mệt mà duyệt.
    cooldown_until  TIMESTAMPTZ,
    crat_ref        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_scope_request_open
    ON omni_admin.scope_request(tenant_id, pattern_key, created_at DESC);

-- Khuôn khổ quyền hạn do admin tenant cấu hình trên portal. Omni ĐỀ XUẤT
-- (proposed_*), khách duyệt — portal không phải form trống.
CREATE TABLE IF NOT EXISTS omni_admin.scope_grant (
    tenant_id     TEXT NOT NULL,
    pattern_key   TEXT NOT NULL,
    granted_scope TEXT NOT NULL DEFAULT 'SUGGEST_ONLY',
    granted_by    TEXT NOT NULL DEFAULT '',
    granted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- FROZEN chỉ NGƯỜI gỡ được. Omni tự lên bậc được, không tự gỡ án được.
    frozen        BOOLEAN NOT NULL DEFAULT FALSE,
    frozen_reason TEXT,
    PRIMARY KEY (tenant_id, pattern_key)
);

-- ── Bất biến cưỡng chế ở tầng DB ─────────────────────────────────────────────
-- Đặt ở DB chứ không phải quy ước Python: đây là bằng chứng khách hàng dùng để
-- trao quyền cho một hệ thống tự động. Một lần refactor bất cẩn ở tầng ứng dụng
-- là mất bất biến, mà không ai phát hiện.

CREATE OR REPLACE FUNCTION omni_admin.case_ledger_guard()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.case_id     IS DISTINCT FROM OLD.case_id
    OR NEW.pattern_key IS DISTINCT FROM OLD.pattern_key
    OR NEW.tenant_id   IS DISTINCT FROM OLD.tenant_id
    OR NEW.posture     IS DISTINCT FROM OLD.posture
    OR NEW.opened_at   IS DISTINCT FROM OLD.opened_at THEN
        RAISE EXCEPTION
            'case_ledger: truong dong bang khong duoc sua (case_id=%)', OLD.case_id
            USING ERRCODE = 'check_violation';
    END IF;

    -- Verdict không quay về UNJUDGED — đã phán quyết thì không xoá dấu vết.
    IF OLD.diagnosis_verdict <> 'UNJUDGED' AND NEW.diagnosis_verdict = 'UNJUDGED' THEN
        RAISE EXCEPTION 'case_ledger: khong the huy phan quyet chan doan (case_id=%)',
            OLD.case_id USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.remedy_verdict <> 'UNJUDGED' AND NEW.remedy_verdict = 'UNJUDGED' THEN
        RAISE EXCEPTION 'case_ledger: khong the huy phan quyet khac phuc (case_id=%)',
            OLD.case_id USING ERRCODE = 'check_violation';
    END IF;

    -- Mọi thay đổi verdict để lại vết, không phụ thuộc tầng ứng dụng nhớ ghi.
    IF NEW.diagnosis_verdict IS DISTINCT FROM OLD.diagnosis_verdict THEN
        INSERT INTO omni_admin.case_verdict_history
            (case_id, tenant_id, field, from_verdict, to_verdict, source, actor, crat_ref)
        VALUES (OLD.case_id, OLD.tenant_id, 'diagnosis',
                OLD.diagnosis_verdict, NEW.diagnosis_verdict,
                COALESCE(NEW.diagnosis_source, ''), COALESCE(NEW.diagnosis_actor, ''),
                NEW.crat_ref);
    END IF;
    IF NEW.remedy_verdict IS DISTINCT FROM OLD.remedy_verdict THEN
        INSERT INTO omni_admin.case_verdict_history
            (case_id, tenant_id, field, from_verdict, to_verdict, source, actor, crat_ref)
        VALUES (OLD.case_id, OLD.tenant_id, 'remedy',
                OLD.remedy_verdict, NEW.remedy_verdict,
                COALESCE(NEW.remedy_source, ''), COALESCE(NEW.remedy_actor, ''),
                NEW.crat_ref);
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_case_ledger_guard ON omni_admin.case_ledger;
CREATE TRIGGER trg_case_ledger_guard
    BEFORE UPDATE ON omni_admin.case_ledger
    FOR EACH ROW EXECUTE FUNCTION omni_admin.case_ledger_guard();
