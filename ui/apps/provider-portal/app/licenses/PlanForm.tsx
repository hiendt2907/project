"use client";

import { useState } from "react";
import type { TenantPlan } from "@/lib/operations";

export function PlanForm({ tenantId, initial }: { tenantId: string; initial: TenantPlan }) {
  const [plan, setPlan] = useState(initial);
  const [state, setState] = useState<"idle" | "saving" | "saved" | "error">("idle");

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setState("saving");
    const response = await fetch(`/api/provider/v1/tenants/${encodeURIComponent(tenantId)}/plan`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(plan),
    });
    setState(response.ok ? "saved" : "error");
  }

  return <form className="aoip-answer" onSubmit={save} data-testid={`plan-form-${tenantId}`}>
    <label className="aoip-muted">Mã gói<input className="aoip-input" value={plan.plan_code} onChange={(e) => setPlan({ ...plan, plan_code: e.target.value })} /></label>
    <label className="aoip-muted">Agent tối đa<input className="aoip-input" type="number" min={0} value={plan.agent_limit} onChange={(e) => setPlan({ ...plan, agent_limit: Number(e.target.value) })} /></label>
    <label className="aoip-muted">Autonomy tối đa<select className="aoip-select" value={plan.autonomy_ceiling} onChange={(e) => setPlan({ ...plan, autonomy_ceiling: e.target.value })}><option value="shadow">shadow</option><option value="assist">assist</option><option value="auto">auto</option></select></label>
    <label className="aoip-muted">Lưu trữ (ngày)<input className="aoip-input" type="number" min={1} value={plan.retention_days} onChange={(e) => setPlan({ ...plan, retention_days: Number(e.target.value) })} /></label>
    <label className="aoip-muted">Hỗ trợ<select className="aoip-select" value={plan.support_tier} onChange={(e) => setPlan({ ...plan, support_tier: e.target.value })}><option value="standard">standard</option><option value="premium">premium</option><option value="enterprise">enterprise</option></select></label>
    <label className="aoip-muted"><input type="checkbox" checked={plan.enabled} onChange={(e) => setPlan({ ...plan, enabled: e.target.checked })} /> Đang hoạt động</label>
    <button className="aoip-btn" type="submit" disabled={state === "saving"}>{state === "saving" ? "Đang lưu" : "Lưu cấu hình"}</button>
    {state === "saved" ? <span className="aoip-state">Đã lưu</span> : null}
    {state === "error" ? <span className="aoip-err">Lưu thất bại</span> : null}
  </form>;
}
