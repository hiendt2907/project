import { headers } from "next/headers";
import { Card, MetricStat } from "@aoip/ui-kit";
import type { ProviderAgent } from "@aoip/shared-types";
import { fetchAgents } from "@/lib/agents";

export default async function ProviderAgentsPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const result = await fetchAgents(cookieHeader);

  if (result.status === "error") {
    return (
      <Card error>
        <div className="aoip-k err">Không tải được Agents</div>
        <div className="aoip-state" data-testid="agents-error">
          Backend trả mã {result.code || "không phản hồi"}. Thử tải lại trang.
        </div>
      </Card>
    );
  }

  const { summary, agents } = result.data;
  return (
    <>
      <div className="aoip-k">Remote Agents</div>
      <div className="aoip-grid" data-testid="agents-summary">
        <MetricStat label="Agents total" value={summary.total} />
        <MetricStat label="Agents online" value={summary.online} />
        <MetricStat label="Agents stale" value={summary.stale} />
        <MetricStat label="Agents offline" value={summary.offline} />
      </div>

      <Card>
        {agents.length === 0 ? (
          <div className="aoip-state" data-testid="agents-empty">
            Chưa có Remote Agent nào register vào runtime.
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
                  <th>Discovery</th>
                  <th>Latest check</th>
                  <th>Command</th>
                </tr>
              </thead>
              <tbody>
                {agents.map((agent) => <AgentRow key={agent.agent_id} agent={agent} />)}
              </tbody>
            </table>
          </div>
        )}
      </Card>
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

function formatAge(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "unknown";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}
