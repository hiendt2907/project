-- Advisory Mode: operator acknowledgment ledger (bổ sung CRAT, để query/UI nhanh).
-- Khác omni_admin.hitl_decision (mutation-approval, NOT NULL tool_name/risk_class/tier) —
-- advisory ack không gắn với mutation nào đang chờ, chỉ ghi nhận operator đã xem/xử lý
-- 1 advisory suggestion gửi qua Telegram (Advisory Mode: escalate qua Telegram, không
-- qua omni-hitl-pending). CRAT (omni-audit-chain, event ADVISORY_DECISION) vẫn là nguồn
-- sự thật bất biến; bảng này chỉ phục vụ đọc nhanh.
CREATE TABLE IF NOT EXISTS omni_admin.advisory_acknowledgment (
    trace_id      TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    actor         TEXT,
    channel       TEXT NOT NULL DEFAULT 'telegram' CHECK (channel IN ('telegram','ui')),
    crat_ref      TEXT,
    acknowledged_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_advisory_ack_tenant
    ON omni_admin.advisory_acknowledgment(tenant_id, acknowledged_at DESC);
