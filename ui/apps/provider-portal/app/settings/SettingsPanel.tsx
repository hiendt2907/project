"use client";

import { useState } from "react";
import type { ProviderAgentCredential, ProviderTenantSummary } from "@/lib/settings";

interface Props {
  tenants: ProviderTenantSummary[];
  agentCredentials: Record<string, ProviderAgentCredential[]>;
}

export function SettingsPanel({ tenants, agentCredentials }: Props) {
  const [byTenant, setByTenant] = useState(agentCredentials);
  const [selectedTenant, setSelectedTenant] = useState(tenants[0]?.tenant_id ?? "");
  const [label, setLabel] = useState("");
  const [issuedToken, setIssuedToken] = useState<string | null>(null);
  const [state, setState] = useState<"idle" | "saving" | "error">("idle");

  async function issueToken(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedTenant) return;
    setState("saving");
    setIssuedToken(null);
    const res = await fetch("/api/provider/v1/settings/enroll-tokens", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ tenant_id: selectedTenant, label: label.trim() || undefined }),
    });
    if (!res.ok) {
      setState("error");
      return;
    }
    const body = (await res.json()) as { enroll_token: string };
    setIssuedToken(body.enroll_token);
    setLabel("");
    setState("idle");
  }

  async function revoke(tenantId: string, agentId: string) {
    const res = await fetch(
      `/api/provider/v1/settings/agent-credentials/${encodeURIComponent(tenantId)}/${encodeURIComponent(agentId)}`,
      { method: "DELETE" },
    );
    if (!res.ok) return;
    setByTenant((prev) => ({
      ...prev,
      [tenantId]: (prev[tenantId] ?? []).map((c) =>
        c.agent_id === agentId ? { ...c, status: "revoked" } : c,
      ),
    }));
  }

  return (
    <>
      <form className="aoip-answer" onSubmit={issueToken}>
        <select
          className="aoip-select"
          value={selectedTenant}
          onChange={(e) => setSelectedTenant(e.target.value)}
        >
          {tenants.map((t) => (
            <option key={t.tenant_id} value={t.tenant_id}>{t.tenant_id}</option>
          ))}
        </select>
        <input
          className="aoip-input"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="Label (vd: cust-app pilot)"
        />
        <button className="aoip-btn" type="submit" disabled={state === "saving" || !selectedTenant}>
          {state === "saving" ? "Đang phát hành" : "Phát hành enroll token"}
        </button>
        {state === "error" ? <span className="aoip-err">Phát hành thất bại</span> : null}
      </form>

      {issuedToken ? (
        <div className="aoip-state" data-testid="issued-token">
          Token (chỉ hiện MỘT lần, copy ngay): <code>{issuedToken}</code>
        </div>
      ) : null}

      {tenants.map((t) => (
        <div key={t.tenant_id} className="aoip-table-wrap">
          <div className="aoip-muted">{t.tenant_id}</div>
          <table className="aoip-table" data-testid={`credentials-${t.tenant_id}`}>
            <thead>
              <tr>
                <th>Agent</th>
                <th>Host</th>
                <th>Key</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {(byTenant[t.tenant_id] ?? []).map((c) => (
                <tr key={c.id}>
                  <td>{c.agent_id}</td>
                  <td>{c.hostname}</td>
                  <td>{c.key_prefix}…</td>
                  <td><span className={`aoip-pill ${c.status === "active" ? "online" : "offline"}`}>{c.status}</span></td>
                  <td>
                    {c.status === "active" ? (
                      <button className="aoip-btn" onClick={() => revoke(t.tenant_id, c.agent_id)}>
                        Revoke
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
              {(byTenant[t.tenant_id] ?? []).length === 0 ? (
                <tr><td colSpan={5} className="aoip-muted">Chưa có credential nào</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>
      ))}
    </>
  );
}
