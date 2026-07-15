"use client";

import { useState } from "react";

export function EnvironmentForm({ tenantId }: { tenantId: string }) {
  const [environmentId, setEnvironmentId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [environmentType, setEnvironmentType] = useState("production");
  const [state, setState] = useState<"idle" | "saving" | "error">("idle");

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!environmentId.trim() || !displayName.trim()) return;
    setState("saving");
    const response = await fetch(
      `/api/provider/v1/tenants/${encodeURIComponent(tenantId)}/environments`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          environment_id: environmentId.trim(),
          display_name: displayName.trim(),
          environment_type: environmentType,
        }),
      },
    );
    if (!response.ok) {
      setState("error");
      return;
    }
    window.location.reload();
  }

  return (
    <form className="aoip-answer" onSubmit={submit} data-testid={`environment-form-${tenantId}`}>
      <input
        className="aoip-input"
        value={environmentId}
        onChange={(event) => setEnvironmentId(event.target.value)}
        placeholder="Mã môi trường (vd: prod)"
        disabled={state === "saving"}
      />
      <input
        className="aoip-input"
        value={displayName}
        onChange={(event) => setDisplayName(event.target.value)}
        placeholder="Tên hiển thị"
        disabled={state === "saving"}
      />
      <select
        className="aoip-select"
        value={environmentType}
        onChange={(event) => setEnvironmentType(event.target.value)}
        disabled={state === "saving"}
      >
        <option value="production">Production</option>
        <option value="staging">Staging</option>
        <option value="development">Development</option>
      </select>
      <button className="aoip-btn" type="submit" disabled={state === "saving"}>
        {state === "saving" ? "Đang tạo" : "Tạo môi trường"}
      </button>
      {state === "error" ? <span className="aoip-err">Không tạo được môi trường</span> : null}
    </form>
  );
}
