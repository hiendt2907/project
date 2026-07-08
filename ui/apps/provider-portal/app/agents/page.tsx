import { headers } from "next/headers";
import { Card, MetricStat } from "@aoip/ui-kit";
import { fetchAgents } from "@/lib/agents";
import { AgentsTable } from "./AgentsTable";

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
        <MetricStat label="Fleet drifted" value={summary.drifted} />
      </div>

      <Card>
        <AgentsTable agents={agents} />
      </Card>
    </>
  );
}
