import { headers } from "next/headers";
import { Card, MetricStat } from "@aoip/ui-kit";
import { fetchAudit } from "@/lib/audit";

// Read-only projection of services.audit_ledger.chain_writer (CRAT tamper-evident
// hash-chain) — every event type (ADVISORY_DECISION/ADVISORY_DISPATCHED/
// MUTATION_TRAPPED/HITL_DECISION/ROLLBACK_EXECUTED), across tenants. Distinct from
// /incidents (SIEM-only verdict view, per-tenant): this is the full compliance chain.
export default async function ProviderAuditPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const result = await fetchAudit(cookieHeader);

  if (result.status === "error") {
    return (
      <Card error>
        <div className="aoip-k err">Không tải được Audit chain</div>
        <div className="aoip-state" data-testid="audit-error">
          Backend trả mã {result.code || "không phản hồi"}. Thử tải lại trang.
        </div>
      </Card>
    );
  }

  const { total, signed, event_counts, blocks } = result.data;
  return (
    <>
      <div className="aoip-k">Audit — CRAT Hash-Chain</div>
      <div className="aoip-grid" data-testid="audit-summary">
        <MetricStat label="Blocks (gần nhất)" value={total} />
        <MetricStat label="Đã ký (signed)" value={signed} />
        {Object.entries(event_counts).map(([type, count]) => (
          <MetricStat key={type} label={type} value={count} />
        ))}
      </div>

      <Card>
        {blocks.length === 0 ? (
          <div className="aoip-state" data-testid="audit-empty">
            Chưa có audit block nào.
          </div>
        ) : (
          <div className="aoip-table-wrap">
            <table className="aoip-table" data-testid="audit-table">
              <thead>
                <tr>
                  <th>Seq</th>
                  <th>Event</th>
                  <th>Tenant</th>
                  <th>Trace</th>
                  <th>Timestamp</th>
                  <th>Hash</th>
                </tr>
              </thead>
              <tbody>
                {blocks.map((b) => (
                  <tr key={`${b.tenant_id}-${b.seq}`} data-testid={`audit-block-${b.tenant_id}-${b.seq}`}>
                    <td>{b.seq}</td>
                    <td>
                      {b.event_type}
                      {b.signed ? <span className="aoip-muted"> · signed</span> : null}
                    </td>
                    <td>{b.tenant_id}</td>
                    <td className="aoip-muted">{b.trace_id || "n/a"}</td>
                    <td className="aoip-muted">{b.timestamp_utc}</td>
                    <td className="aoip-muted">{b.block_hash.slice(0, 12)}…</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
