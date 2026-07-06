import { headers } from "next/headers";
import { Card, MetricStat } from "@aoip/ui-kit";
import { fetchUnderstanding } from "@/lib/understanding";
import { fetchSiemOverview, type SiemOverviewResponse } from "@/lib/siem";

// Read-only projection of src/gateway/routes/siem.py `/siem/overview` — CRAT audit-chain
// summary (verdict distribution, recent blocks), tenant-scoped. No write-action here:
// HITL decide / kill-chain correlation are separate backend surfaces, out of scope for
// this slice (see docs/plans/aoip-provider-portal-slices.md).
export default async function ProviderIncidentsPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const understanding = await fetchUnderstanding(cookieHeader);

  if (understanding.status === "error") {
    return (
      <Card error>
        <div className="aoip-k err">Không tải được danh sách tenant</div>
        <div className="aoip-state" data-testid="incidents-error">
          Backend trả mã {understanding.code || "không phản hồi"}. Thử tải lại trang.
        </div>
      </Card>
    );
  }

  const tenants = understanding.data.tenants;
  const overviews = await Promise.all(
    tenants.map((t) => fetchSiemOverview(t.tenant_id)),
  );

  return (
    <>
      <div className="aoip-k">Incidents</div>
      {tenants.length === 0 ? (
        <Card>
          <div className="aoip-state" data-testid="incidents-empty">
            Chưa có tenant nào trong runtime.
          </div>
        </Card>
      ) : (
        tenants.map((tenant, i) => (
          <TenantIncidents
            key={tenant.tenant_id}
            tenantId={tenant.tenant_id}
            result={overviews[i]}
          />
        ))
      )}
    </>
  );
}

function TenantIncidents({
  tenantId,
  result,
}: {
  tenantId: string;
  result: { data: SiemOverviewResponse | null; error: string | null };
}) {
  if (result.error || !result.data) {
    return (
      <Card error>
        <div className="aoip-k err">{tenantId}</div>
        <div className="aoip-state" data-testid={`incidents-error-${tenantId}`}>
          {result.error ?? "Không có dữ liệu"}
        </div>
      </Card>
    );
  }

  const { chain, verdict_distribution_24h, recent_blocks } = result.data;
  return (
    <Card>
      <div className="aoip-k">{tenantId}</div>
      <div className="aoip-grid" data-testid={`incidents-summary-${tenantId}`}>
        <MetricStat label="Audit blocks" value={chain.total_blocks} />
        <MetricStat label="Chain integrity" value={chain.integrity} />
        {Object.entries(verdict_distribution_24h).map(([verdict, count]) => (
          <MetricStat key={verdict} label={`Verdict · ${verdict}`} value={count} />
        ))}
      </div>

      {recent_blocks.length === 0 ? (
        <div className="aoip-state">Chưa có audit block nào gần đây.</div>
      ) : (
        recent_blocks.slice(0, 20).map((block) => (
          <div
            key={`${block.seq}-${block.trace_id}`}
            className="aoip-question"
            data-testid={`incident-block-${tenantId}-${block.seq}`}
          >
            <div className="aoip-row">
              <span>{block.event_type} · {block.affected_workload || "n/a"}</span>
              <span className="aoip-chip-row">
                <span className="aoip-pill active">{block.verdict}</span>
              </span>
            </div>
            {block.root_cause ? <div>{block.root_cause}</div> : null}
            <div className="aoip-muted">
              {block.timestamp_utc} · trace={block.trace_id ?? "n/a"} · hash={block.block_hash}
            </div>
          </div>
        ))
      )}
    </Card>
  );
}
