"use client";

import { useState } from "react";
import type { MutationToggleResponse } from "@/lib/mutation";

export function MutationToggle({
  tenantId,
  initial,
}: {
  tenantId: string;
  initial: { data: MutationToggleResponse | null; error: string | null };
}) {
  const [status, setStatus] = useState(initial.data);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initial.error);

  async function toggle() {
    if (!status) return;
    if (!status.requested && !confirming) {
      setConfirming(true);
      return;
    }
    setBusy(true);
    setError(null);
    const enabled = !status.requested;
    try {
      const response = await fetch("/api/gateway/autonomy/mutation", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ tenant_id: tenantId, enabled, actor: "provider_portal", confirm: enabled }),
      });
      const body = await response.json() as MutationToggleResponse & { detail?: string; error?: string };
      if (!response.ok) throw new Error(body.detail || body.error || `HTTP ${response.status}`);
      setStatus(body);
      setConfirming(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không cập nhật được quyền mutation");
    } finally {
      setBusy(false);
    }
  }

  if (!status) return <div className="aoip-state">Không đọc được trạng thái mutation{error ? ` · ${error}` : ""}</div>;

  return (
    <div className="aoip-toggle-panel" data-testid={`mutation-toggle-${tenantId}`}>
      <div>
        <div className="aoip-k">Quyền tự động khắc phục · {tenantId}</div>
        <div className="aoip-toggle-title">
          <span className={`aoip-toggle-dot ${status.effective ? "on" : "off"}`} />
          {status.effective ? "Đang cho phép mutation" : "Đang khóa mutation"}
        </div>
        <div className="aoip-muted">
          Tenant switch: {status.requested ? "ON" : "OFF"} · Master kill-switch: {status.master_kill_switch ? "ON" : "OFF"} · {status.reason}
        </div>
        {error ? <div className="aoip-err">{error}</div> : null}
      </div>
      <div className="aoip-toggle-actions">
        {confirming ? <span className="aoip-toggle-warning">Bấm lần nữa để xác nhận bật quyền.</span> : null}
        <button
          className={`aoip-switch ${status.requested ? "on" : "off"}`}
          type="button"
          aria-pressed={status.requested}
          aria-label={`${status.requested ? "Tắt" : "Bật"} quyền mutation cho ${tenantId}`}
          disabled={busy}
          onClick={toggle}
        >
          <span className="aoip-switch-thumb" />
          <span>{busy ? "Đang lưu" : confirming ? "Xác nhận bật" : status.requested ? "Bật" : "Tắt"}</span>
        </button>
      </div>
    </div>
  );
}
