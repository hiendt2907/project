import { type NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export type IncidentStatus = "ACTIVE" | "HITL_PENDING" | "RESOLVED" | "FAILED";
export type IncidentSeverity = "critical" | "high" | "medium" | "low";
export type IncidentLane = "SYS_RESOURCE" | "SYS_HARD_FAIL" | "APP_HTTP" | "SIEM_SECURITY";

export interface Incident {
  id: string;
  timestamp: string;
  lane: IncidentLane;
  severity: IncidentSeverity;
  verdict: string;
  status: IncidentStatus;
  playbook_id: string | null;
  hitl_incident_id?: string;
  trace_id: string;
  summary: string;
  events: { timestamp: string; message: string }[];
  crat_hash: string;
}

export interface IncidentsResponse {
  incidents: Incident[];
  total: number;
  hitl_pending_count: number;
  source: "gateway";
  generated_at: string;
}

// Map CRAT event_type to diagnostic lane
const EVENT_TYPE_LANE: Record<string, IncidentLane> = {
  ADVISORY_DECISION: "SYS_HARD_FAIL",
  ADVISORY_DISPATCHED: "SYS_HARD_FAIL",
  MUTATION_TRAPPED: "SYS_RESOURCE",
  HITL_DECISION: "SIEM_SECURITY",
  ROLLBACK_EXECUTED: "APP_HTTP",
  SOP_PROMOTED: "SYS_HARD_FAIL",
};

function verdictToSeverity(verdict: string): IncidentSeverity {
  const v = verdict.toUpperCase();
  if (v === "CRITICAL" || v === "INVESTIGATE") return "critical";
  if (v === "URGENT") return "high";
  if (v === "SUGGEST_REMEDIATION") return "medium";
  return "low";
}

function eventTypeToStatus(eventType: string): IncidentStatus {
  if (eventType === "ADVISORY_DECISION") return "ACTIVE";
  if (eventType === "HITL_DECISION") return "HITL_PENDING";
  if (eventType === "ADVISORY_DISPATCHED") return "RESOLVED";
  if (eventType === "MUTATION_TRAPPED") return "FAILED";
  return "RESOLVED";
}

interface CratBlock {
  seq: number;
  event_type: string;
  trace_id: string;
  timestamp_utc: string;
  verdict: string;
  root_cause: string;
  affected_workload: string;
  block_hash: string;
}

function blocksToIncidents(blocks: CratBlock[]): Incident[] {
  return blocks.map((b) => ({
    id: b.trace_id || `seq-${b.seq}`,
    timestamp: b.timestamp_utc,
    lane: EVENT_TYPE_LANE[b.event_type] ?? "SYS_HARD_FAIL",
    severity: verdictToSeverity(b.verdict ?? ""),
    verdict: b.verdict ?? "UNKNOWN",
    status: eventTypeToStatus(b.event_type),
    playbook_id: null,
    trace_id: b.trace_id,
    summary: [b.root_cause, b.affected_workload].filter(Boolean).join(" — ") || b.event_type,
    events: [{ timestamp: b.timestamp_utc, message: b.event_type }],
    crat_hash: b.block_hash ?? "",
  }));
}

function authHeaders(): HeadersInit {
  return GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {};
}

function gatewayError(detail: string) {
  return NextResponse.json({ source: "error", error: detail }, { status: 502 });
}

export async function GET(request: NextRequest) {
  const tenantId = request.nextUrl.searchParams.get("tenant_id");
  const tenantParam = tenantId ? `&tenant_id=${encodeURIComponent(tenantId)}` : "";

  if (!GATEWAY_URL) {
    return gatewayError("OMNI_GATEWAY_URL not configured");
  }

  try {
    const res = await fetch(`${GATEWAY_URL}/siem/overview?limit=50${tenantParam}`, {
      headers: authHeaders(),
      cache: "no-store",
    });

    if (!res.ok) return gatewayError(`gateway /siem/overview ${res.status}`);

    const data = await res.json();
    const blocks: CratBlock[] = (data.recent_blocks ?? []) as CratBlock[];
    const incidents = blocksToIncidents(blocks);
    const hitl_pending_count = incidents.filter((i) => i.status === "HITL_PENDING").length;

    return NextResponse.json({
      incidents,
      total: incidents.length,
      hitl_pending_count,
      source: "gateway",
      generated_at: new Date().toISOString(),
    } satisfies IncidentsResponse);
  } catch {
    return gatewayError("gateway /siem/overview unreachable");
  }
}
