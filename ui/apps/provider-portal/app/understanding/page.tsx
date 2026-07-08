import { headers } from "next/headers";
import { Card, MetricStat } from "@aoip/ui-kit";
import type { ProviderTenantUnderstanding, ProviderTwinFact } from "@aoip/shared-types";
import { fetchUnderstanding } from "@/lib/understanding";
import { fetchReadiness, type ReadinessResponse } from "@/lib/readiness";
import { fetchDiagram, type DiagramResponse } from "@/lib/diagram";
import { MermaidBlock } from "@/components/mermaid-diagram";
import { splitDiagramText } from "@/lib/diagram-utils";
import { DiagramHistoryPanel } from "./DiagramHistoryPanel";
import "./understanding.css";

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
  // Readiness + system diagram live on the Omni gateway (/onboarding/*), not the
  // provider console API — fetched separately per tenant, in parallel.
  const [readinessByTenant, diagramByTenant] = await Promise.all([
    Promise.all(tenants.map((t) => fetchReadiness(t.tenant_id))),
    Promise.all(tenants.map((t) => fetchDiagram(t.tenant_id))),
  ]);

  return (
    <>
      <div className="aoip-k">System Understanding</div>
      {tenants.length === 0 ? (
        <Card>
          <div className="aoip-state" data-testid="understanding-empty">
            Chưa có System Twin nào trong runtime.
          </div>
        </Card>
      ) : tenants.map((tenant, i) => (
        <TenantUnderstanding
          key={tenant.tenant_id}
          tenant={tenant}
          readiness={readinessByTenant[i]}
          diagram={diagramByTenant[i]}
        />
      ))}
    </>
  );
}

function TenantUnderstanding({ tenant, readiness, diagram }: {
  tenant: ProviderTenantUnderstanding;
  readiness: { data: ReadinessResponse | null; error: string | null };
  diagram: { data: DiagramResponse | null; error: string | null };
}) {
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

      <ReadinessCard readiness={readiness} testid={`readiness-${tenant.tenant_id}`} />
      <DiagramCard diagram={diagram} tenantId={tenant.tenant_id} testid={`diagram-${tenant.tenant_id}`} />

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

function formatUpdatedAt(iso: string | null): string | null {
  if (!iso) return null;
  const ts = Date.parse(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  if (Number.isNaN(ts)) return null;
  return `${formatAge(Math.max(0, Math.floor((Date.now() - ts) / 1000)))} ago`;
}

interface ReadinessCheckProps {
  label: string;
  detail: string;
  pass: boolean;
  progressPct?: number | null;
  targetPct?: number;
}

function ReadinessCheck({ label, detail, pass, progressPct, targetPct }: ReadinessCheckProps) {
  const hasBar = progressPct !== undefined && progressPct !== null && targetPct !== undefined;
  return (
    <div className="aoip-check">
      <div className="aoip-check-body">
        <div className="aoip-check-head">
          <span>{label}</span>
          <span className={`aoip-check-status ${pass ? "pass" : "fail"}`}>
            {pass ? "Done" : "Needs work"}
          </span>
        </div>
        <div className="aoip-muted">{detail}</div>
        {hasBar && (
          <div className="aoip-progress">
            <div
              className={`aoip-progress-bar ${pass ? "" : "fail"}`}
              style={{ width: `${Math.min(100, Math.max(0, progressPct))}%` }}
            />
            <div
              className="aoip-progress-target"
              style={{ left: `${Math.min(100, Math.max(0, targetPct))}%` }}
              title={`Target ${targetPct}%`}
            />
          </div>
        )}
      </div>
    </div>
  );
}

// Understanding Readiness — whether Omni knows this tenant's system well enough
// to assist with confidence. Ported from ui/app/understanding/page.tsx
// (Productization iteration 26). Data source: gateway GET /onboarding/readiness.
function ReadinessCard({ readiness, testid }: {
  readiness: { data: ReadinessResponse | null; error: string | null };
  testid: string;
}) {
  const record = readiness.data?.readiness ?? null;
  const thresholds = readiness.data?.thresholds ?? null;
  return (
    <Card>
      <div className="aoip-check-head" style={{ marginBottom: 8 }}>
        <div className="aoip-k" style={{ marginBottom: 0 }}>Understanding Readiness</div>
        {record && (
          <span className={`aoip-check-status ${record.readiness_flag ? "pass" : "fail"}`}>
            {record.readiness_flag ? "Ready" : "Not ready yet"}
          </span>
        )}
      </div>
      {readiness.error ? (
        <div className="aoip-state" data-testid={`${testid}-error`}>{readiness.error}</div>
      ) : record && thresholds ? (
        <div data-testid={testid}>
          <ReadinessCheck
            label="Endpoints mapped"
            detail={`${Math.round(record.endpoint_mapped_pct ?? 0)}% of discovered endpoints understood — target ${Math.round(thresholds.endpoint_mapped_pct_min)}%`}
            pass={(record.endpoint_mapped_pct ?? 0) >= thresholds.endpoint_mapped_pct_min}
            progressPct={record.endpoint_mapped_pct}
            targetPct={thresholds.endpoint_mapped_pct_min}
          />
          <ReadinessCheck
            label="Business flows confirmed"
            detail={`${Math.round(record.business_flow_confirmed_pct ?? 0)}% of business flows confirmed by a human — target ${Math.round(thresholds.business_flow_confirmed_pct_min)}%`}
            pass={(record.business_flow_confirmed_pct ?? 0) >= thresholds.business_flow_confirmed_pct_min}
            progressPct={record.business_flow_confirmed_pct}
            targetPct={thresholds.business_flow_confirmed_pct_min}
          />
          <ReadinessCheck
            label="Stale open questions"
            detail={
              record.open_questions_over_threshold === 0
                ? `No question has waited longer than ${thresholds.open_question_stale_days} days`
                : `${record.open_questions_over_threshold} question(s) unanswered for over ${thresholds.open_question_stale_days} days`
            }
            pass={record.open_questions_over_threshold <= thresholds.open_questions_max}
          />
          {formatUpdatedAt(record.updated_at) && (
            <div className="aoip-muted">Last evaluated {formatUpdatedAt(record.updated_at)}</div>
          )}
        </div>
      ) : (
        <div className="aoip-state" data-testid={`${testid}-empty`}>
          No readiness record for this tenant yet. It appears after the first discovery cycle or
          handover-doc upload triggers a readiness evaluation.
        </div>
      )}
    </Card>
  );
}

// System entity graph (Mermaid) — proxies_to/depends_on/connects_to edges from
// pkg.onboarding.discovery_doc. Ported from ui/components/mermaid-diagram.tsx
// (Productization iteration 22). Data source: gateway GET /onboarding/diagram.
// Mirrors aoip.system_graph.NODE_TYPE_PREFIX / _TOPOLOGY_NODE_SHAPES
// (src/pkg/onboarding/discovery_doc.py render_system_topology_diagram) —
// keep in sync if either side changes shape/type mapping.
const DIAGRAM_LEGEND: { cls: string; label: string }[] = [
  { cls: "host", label: "Host" },
  { cls: "svc", label: "Service" },
  { cls: "api", label: "API" },
  { cls: "db", label: "Database" },
  { cls: "doc", label: "Document" },
];

function DiagramLegend() {
  return (
    <div className="aoip-diagram-legend" data-testid="diagram-legend">
      {DIAGRAM_LEGEND.map((item) => (
        <span className="aoip-diagram-legend-item" key={item.cls}>
          <span className={`aoip-diagram-legend-shape ${item.cls}`} />
          {item.label}
        </span>
      ))}
    </div>
  );
}

function DiagramCard({ diagram, tenantId, testid }: {
  diagram: { data: DiagramResponse | null; error: string | null };
  tenantId: string;
  testid: string;
}) {
  const sections = diagram.data?.mermaid ? splitDiagramText(diagram.data.mermaid) : [];
  return (
    <Card>
      <div className="aoip-check-head" style={{ marginBottom: 8 }}>
        <div className="aoip-k" style={{ marginBottom: 0 }}>System Diagram</div>
        {diagram.data?.version != null && (
          <span className="aoip-muted">v{diagram.data.version}</span>
        )}
      </div>
      {diagram.error ? (
        <div className="aoip-state" data-testid={`${testid}-error`}>{diagram.error}</div>
      ) : sections.length === 0 ? (
        <div className="aoip-state" data-testid={`${testid}-empty`}>
          No diagram generated for this tenant yet.
        </div>
      ) : (
        <div data-testid={testid}>
          <DiagramLegend />
          {sections.map((section) => (
            <div key={section.title} style={{ marginBottom: 12 }}>
              <div className="aoip-diagram-title">{section.title}</div>
              <MermaidBlock source={section.source} />
            </div>
          ))}
          <DiagramHistoryPanel tenant={tenantId} />
        </div>
      )}
    </Card>
  );
}
