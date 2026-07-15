import { headers } from "next/headers";
import { Card, MetricStat } from "@aoip/ui-kit";
import { fetchAgents } from "@/lib/agents";

export default async function TenantAgentsPage() {
  const result = await fetchAgents((await headers()).get("cookie") ?? "");
  if (result.status === "error") {
    return <Card error><div className="aoip-k err">Không tải được Agent</div>
      <div className="aoip-state">Backend trả mã {result.code || "không phản hồi"}.</div></Card>;
  }
  const { summary, agents } = result.data;
  return <>
    <h1>Agent của tôi</h1>
    <p className="aoip-muted">Các agent đang vận hành trong tổ chức của bạn.</p>
    <div className="aoip-grid">
      <MetricStat label="Tổng số" value={summary.total} />
      <MetricStat label="Online" value={summary.online} />
      <MetricStat label="Stale" value={summary.stale} />
      <MetricStat label="Offline" value={summary.offline} />
    </div>
    <Card>
      {agents.length === 0 ? <div className="aoip-state">Chưa có agent đăng ký.</div> :
        <div className="aoip-table-wrap"><table className="aoip-table">
          <thead><tr><th>Agent</th><th>Host</th><th>Trạng thái</th><th>Runtime</th><th>Discovery</th><th>Command</th></tr></thead>
          <tbody>{agents.map((agent) => <tr key={agent.agent_id}>
            <td><div>{agent.agent_id}</div><div className="aoip-muted">v{agent.version} · {agent.platform}</div></td>
            <td>{agent.hostname}</td>
            <td><span className={`aoip-pill ${agent.status}`}>{agent.status}</span></td>
            <td>{agent.runtime}</td>
            <td>{agent.discovery_enabled ? "enabled" : "disabled"}</td>
            <td>{agent.command_state} · {agent.pending_commands} pending</td>
          </tr>)}</tbody>
        </table></div>}
    </Card>
  </>;
}
