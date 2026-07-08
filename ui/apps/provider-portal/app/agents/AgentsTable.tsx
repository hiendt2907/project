"use client";

import { useMemo, useState } from "react";
import type { ProviderAgent } from "@aoip/shared-types";

// Tenant filter — ported from ui/app/remote-agents (omni-ui) parity gap: đó là
// mock/single-purpose gateway list, ở đây agents[].tenant_id đã có sẵn từ
// build_provider_agents() nên filter chạy client-side, không cần round-trip.
export function AgentsTable({ agents }: { agents: ProviderAgent[] }) {
  const [tenantFilter, setTenantFilter] = useState<string>("all");

  const tenantIds = useMemo(
    () => Array.from(new Set(agents.map((a) => a.tenant_id))).sort(),
    [agents],
  );

  const filtered = tenantFilter === "all"
    ? agents
    : agents.filter((a) => a.tenant_id === tenantFilter);

  if (agents.length === 0) {
    return (
      <div className="aoip-state" data-testid="agents-empty">
        Chưa có Remote Agent nào register vào runtime.
      </div>
    );
  }

  return (
    <>
      <div className="aoip-filter-row">
        <label htmlFor="agents-tenant-filter" className="aoip-muted">Tenant</label>
        <select
          id="agents-tenant-filter"
          className="aoip-select"
          data-testid="agents-tenant-filter"
          value={tenantFilter}
          onChange={(e) => setTenantFilter(e.target.value)}
        >
          <option value="all">Tất cả ({agents.length})</option>
          {tenantIds.map((tid) => (
            <option key={tid} value={tid}>
              {tid} ({agents.filter((a) => a.tenant_id === tid).length})
            </option>
          ))}
        </select>
      </div>

      {filtered.length === 0 ? (
        <div className="aoip-state" data-testid="agents-filter-empty">
          Không có agent nào cho tenant &quot;{tenantFilter}&quot;.
        </div>
      ) : (
        <div className="aoip-table-wrap">
          <table className="aoip-table" data-testid="agents-table">
            <thead>
              <tr>
                <th>Agent</th>
                <th>Tenant</th>
                <th>Host</th>
                <th>Status</th>
                <th>Runtime</th>
                <th>Discovery</th>
                <th>Latest check</th>
                <th>Command</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((agent) => <AgentRow key={agent.agent_id} agent={agent} />)}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function AgentRow({ agent }: { agent: ProviderAgent }) {
  const check = agent.last_discovery_result;
  return (
    <tr data-testid={`agent-${agent.agent_id}`}>
      <td>
        <div>{agent.agent_id}</div>
        <div className="aoip-muted">v{agent.version} · {agent.platform}</div>
      </td>
      <td>{agent.tenant_id}</td>
      <td>{agent.hostname}</td>
      <td>
        <span className={`aoip-pill ${agent.status}`}>{agent.status}</span>
        <div className="aoip-muted">{formatAge(agent.age_seconds)} ago</div>
      </td>
      <td>
        <span className={`aoip-pill ${agent.drift_status}`}>{agent.drift_status}</span>
        <div className="aoip-muted">
          {agent.runtime === "employee" ? "employee" : "legacy"} · {shortHash(agent.bundle_sha256)}
        </div>
      </td>
      <td>
        <span className={agent.discovery_enabled ? "aoip-ok" : "aoip-muted"}>
          {agent.discovery_enabled ? "enabled" : "disabled"}
        </span>
        <div className="aoip-muted">{agent.evidence_count} evidence</div>
      </td>
      <td>
        {check ? (
          <>
            <div>{check.probe} · {check.result}</div>
            <div className="aoip-muted">{check.summary || "no summary"}</div>
          </>
        ) : (
          <span className="aoip-muted">no checks yet</span>
        )}
      </td>
      <td>
        <span className={`aoip-pill ${agent.command_state}`}>{agent.command_state}</span>
        <div className="aoip-muted">{agent.pending_commands} pending</div>
      </td>
    </tr>
  );
}

function shortHash(hash: string): string {
  return hash ? hash.slice(0, 8) : "no hash";
}

function formatAge(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "unknown";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}
