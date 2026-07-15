"use client";

import { FormEvent, useState } from "react";

export function TenantCreateForm() {
  const [tenantId, setTenantId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setMessage("");
    try {
      const response = await fetch("/api/provider/v1/tenants", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ tenant_id: tenantId, display_name: displayName }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(body.detail || `HTTP ${response.status}`));
      setMessage("Đã tạo tenant. Đang tải lại…"); window.location.reload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không tạo được tenant");
    } finally { setBusy(false); }
  }

  return <form onSubmit={submit} className="aoip-form" data-testid="tenant-create-form">
    <div className="aoip-k">Thêm khách hàng</div>
    <div className="aoip-form-row">
      <input className="aoip-input" required maxLength={128} value={tenantId}
        onChange={(e) => setTenantId(e.target.value)} placeholder="Mã tenant (vd: acme)" />
      <input className="aoip-input" required maxLength={256} value={displayName}
        onChange={(e) => setDisplayName(e.target.value)} placeholder="Tên hiển thị" />
      <button className="aoip-btn" disabled={busy}>{busy ? "Đang tạo…" : "Tạo tenant"}</button>
    </div>
    {message && <div className="aoip-state">{message}</div>}
  </form>;
}
