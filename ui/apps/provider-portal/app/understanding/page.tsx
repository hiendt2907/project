import { headers } from "next/headers";
import { Card, MetricStat } from "@aoip/ui-kit";
import type { ProviderTenantUnderstanding, ProviderTwinFact } from "@aoip/shared-types";
import { fetchUnderstanding } from "@/lib/understanding";

export default async function ProviderUnderstandingPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const result = await fetchUnderstanding(cookieHeader);

  if (result.status === "error") {
    return (
      <Card error>
        <div className="aoip-k err">Không tải được Understanding</div>
        <div className="aoip-state" data-testid="understanding-error">
          Backend trả mã {result.code || "không phản hồi"}. Thử tải lại trang.
        </div>
      </Card>
    );
  }

  const tenants = result.data.tenants;
  return (
    <>
      <div className="aoip-k">System Understanding</div>
      {tenants.length === 0 ? (
        <Card>
          <div className="aoip-state" data-testid="understanding-empty">
            Chưa có System Twin nào trong runtime.
          </div>
        </Card>
      ) : tenants.map((tenant) => <TenantUnderstanding key={tenant.tenant_id} tenant={tenant} />)}
    </>
  );
}

function TenantUnderstanding({ tenant }: { tenant: ProviderTenantUnderstanding }) {
  return (
    <section data-testid={`understanding-${tenant.tenant_id}`}>
      <div className="aoip-grid">
        <MetricStat label="Tenant" value={tenant.tenant_id} />
        <MetricStat label="Twin revision" value={tenant.twin.revision} />
        <MetricStat label="Entities" value={tenant.twin.entity_count} />
        <MetricStat label="Facts" value={tenant.twin.fact_count} />
        <MetricStat label="Relationships" value={tenant.twin.relationship_count} />
        <MetricStat label="Unknowns" value={tenant.unknown_count} />
        <MetricStat label="Contradictions" value={tenant.contradiction_count} />
      </div>

      <Card>
        <div className="aoip-k">Entities</div>
        <div className="aoip-chip-row">
          {tenant.entities.length === 0
            ? <span className="aoip-muted">no entities</span>
            : tenant.entities.map((e) => <span className="aoip-chip" key={e}>{e}</span>)}
        </div>
      </Card>

      <Card>
        <div className="aoip-k">Competency Matrix</div>
        {tenant.competency.length === 0 ? (
          <div className="aoip-state">Chưa có entity đủ điều kiện tính competency.</div>
        ) : (
          <div className="aoip-table-wrap">
            <table className="aoip-table" data-testid="competency-table">
              <thead>
                <tr><th>Entity</th><th>Type</th><th>Coverage</th><th>Critical unknowns</th><th>Contradictions</th></tr>
              </thead>
              <tbody>
                {tenant.competency.map((c) => (
                  <tr key={`${c.entity_type}:${c.entity_id}`}>
                    <td>{c.entity_id}</td>
                    <td>{c.entity_type}</td>
                    <td>{c.coverage.coverage_pct}%</td>
                    <td>{c.critical_unknowns.length ? c.critical_unknowns.join(", ") : "none"}</td>
                    <td>{c.contradicted_facets.length ? c.contradicted_facets.join(", ") : "none"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <FactTable title="Relationships" facts={tenant.relationships} testid="relationships-table" />
      <FactTable title="Facts + provenance" facts={tenant.facts} testid="facts-table" />

      <Card>
        <div className="aoip-k">Unknowns and contradictions</div>
        <div className="aoip-row">
          <span>Unknown records</span>
          <span>{tenant.unknown_count}</span>
        </div>
        <div className="aoip-row">
          <span>Question records</span>
          <span>{tenant.question_count}</span>
        </div>
        <div className="aoip-row">
          <span>Contradiction records</span>
          <span>{tenant.contradiction_count}</span>
        </div>
        {tenant.twin.unknown_edge_targets.length > 0 ? (
          <div className="aoip-muted">
            Open graph targets: {tenant.twin.unknown_edge_targets.join(", ")}
          </div>
        ) : null}
      </Card>
    </section>
  );
}

function FactTable({ title, facts, testid }: { title: string; facts: ProviderTwinFact[]; testid: string }) {
  return (
    <Card>
      <div className="aoip-k">{title}</div>
      {facts.length === 0 ? (
        <div className="aoip-state">Không có dữ liệu.</div>
      ) : (
        <div className="aoip-table-wrap">
          <table className="aoip-table" data-testid={testid}>
            <thead>
              <tr><th>Subject</th><th>Predicate</th><th>Object</th><th>Confidence</th><th>Provenance</th><th>Freshness</th></tr>
            </thead>
            <tbody>
              {facts.map((f) => (
                <tr key={`${f.subject}:${f.predicate}:${f.object}`}>
                  <td>{f.subject}</td>
                  <td>{f.predicate}</td>
                  <td>{f.object}</td>
                  <td>{Math.round(f.confidence * 100)}%</td>
                  <td>{f.provenance.join(" · ")}</td>
                  <td>{formatAge(f.freshness_seconds)} ago</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
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
