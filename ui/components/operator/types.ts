// Operator dashboard shared types + status tokens.

import type { OperatorLane, OperatorStatus, OperatorIncident } from "@/mocks/operator-mock";
import type { Incident, IncidentStatus } from "@/app/api/incidents/route";

export type { OperatorLane, OperatorStatus, OperatorIncident };

export interface HitlItem {
  incident_id: string;
  category: string;
  severity: string;
  waiting_sec: number;
  trace_id: string;
}

export interface KpiData {
  acceptance_rate: number | null;
  false_positive_rate: number | null;
  total_24h: number;
  accepted: number;
  trend_by_lane: { lane: string; detected: number; resolved: number }[];
  source: "gateway" | "mock";
}

export interface SiemCorrelation {
  chains_detected_24h: number;
  active_windows: number;
  chains_by_category: { category: string; count: number }[];
}

export interface SiemPlaybook {
  matches_24h: number;
  auto_executed: number;
  hitl_gated: number;
  no_match: number;
}

export interface SiemPipeline {
  kafka_lag: { topic: string; group: string; lag: number }[];
  redis_ops_per_sec: number;
  redis_memory_used_bytes: number;
  redis_memory_max_bytes: number;
}

export const STATUS_LABEL: Record<OperatorStatus, string> = {
  ACTIVE: "ACTIVE",
  HITL_PENDING: "HITL",
  RESOLVED: "OK",
  SUGGEST_ONLY: "SUGGEST",
};

export const STATUS_COLOR: Record<OperatorStatus, string> = {
  ACTIVE: "text-amber-400",
  HITL_PENDING: "text-rose-400",
  RESOLVED: "text-emerald-400",
  SUGGEST_ONLY: "text-sky-400",
};

export const SEV_COLOR: Record<string, string> = {
  critical: "text-rose-500",
  high: "text-orange-400",
  medium: "text-amber-400",
  low: "text-zinc-500",
};

export type HitlDecisionState = Record<string, "pending" | "approved" | "rejected" | "error">;

// ── Incident mapping ──────────────────────────────────────────────────────────

const STATUS_MAP: Record<IncidentStatus, OperatorStatus> = {
  ACTIVE: "ACTIVE",
  HITL_PENDING: "HITL_PENDING",
  RESOLVED: "RESOLVED",
  FAILED: "ACTIVE",
};

function extractAlertname(summary: string): string {
  const byDash = summary.split("—")[0]?.trim();
  if (byDash && byDash.length < 50) return byDash;
  return summary.length > 45 ? summary.slice(0, 45) + "…" : summary;
}

function extractWorkload(summary: string): string {
  const m =
    summary.match(/^\[([^\]]+)\]/) ??
    summary.match(/ on ([a-zA-Z0-9-_.]+)\s/) ??
    summary.match(/ in ([a-zA-Z0-9-_.]+)\s/);
  return m ? m[1] : "unknown";
}

export function mapIncident(inc: Incident): OperatorIncident {
  const ageMs = Date.now() - new Date(inc.timestamp).getTime();
  return {
    id: inc.id,
    trace_id: inc.trace_id,
    lane: inc.lane as OperatorLane,
    severity: inc.severity as OperatorIncident["severity"],
    status: STATUS_MAP[inc.status] ?? "ACTIVE",
    alertname: extractAlertname(inc.summary),
    namespace: "multi-agent",
    workload: extractWorkload(inc.summary),
    timestamp: inc.timestamp,
    age_s: Math.max(0, Math.floor(ageMs / 1000)),
    root_cause: inc.summary,
    verification_steps: inc.events.slice(0, 4).map((e) => ({
      layer: "INFO",
      command: e.message,
      rationale: new Date(e.timestamp).toLocaleTimeString(),
    })),
    suggested_action: inc.events.length > 0 ? inc.events[inc.events.length - 1].message : "Review advisory output in Telegram",
    hitl_id: inc.hitl_incident_id,
  };
}

export function kpiFromResponse(data: {
  advisory: { accepted: number; rejected: number; total: number; acceptance_rate: number | null };
  execution: { false_positive: number; false_positive_rate: number | null };
  trend: { lane: string; detected: number; resolved: number }[];
  source?: string;
}): KpiData {
  return {
    acceptance_rate: data.advisory.acceptance_rate,
    false_positive_rate: data.execution.false_positive_rate,
    total_24h: data.advisory.total,
    accepted: data.advisory.accepted,
    trend_by_lane: data.trend,
    source: data.source === "gateway" ? "gateway" : "mock",
  };
}
