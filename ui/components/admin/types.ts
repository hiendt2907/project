// Admin dashboard shared types.

export interface AgentLogEntry {
  ts: string;
  probe: string;
  result: "PASSED" | "FAILED" | "WARN" | "INCONCLUSIVE";
  alert_hint: string;
  lane: string;
}

export interface PodInfo {
  name: string;
  status: "healthy" | "degraded" | "unhealthy";
  ready: string;
  hb: string;
  error_count?: number;
}

export interface DeployEntry {
  name: string;
  role: string;
  version: string;
  status: "running" | "degraded" | "down";
  last_deployed: string;
  replicas: number;
}

export interface KpiSummary {
  acceptance_rate: number | null;
  false_positive_rate: number | null;
  accepted: number;
  total: number;
  fp_count: number;
  trend: { lane: string; detected: number; resolved: number }[];
  source: "gateway" | "mock";
}

export interface SiemTelemetry {
  llm: {
    total_calls_24h: number | null;
    success_rate: number | null;
    latency_p50_ms: number | null;
    latency_p95_ms: number | null;
    tokens_in_total: number | null;
    tokens_out_total: number | null;
  };
  rag: {
    queries_24h: number | null;
    cache_hit_ratio: number | null;
    avg_query_latency_ms: number | null;
  };
  pipeline: {
    kafka_lag: { topic: string; group: string; lag: number }[];
    redis_ops_per_sec: number | null;
    redis_memory_used_bytes: number | null;
    redis_memory_max_bytes: number | null;
  };
  source: "prometheus" | "error";
}

export const EVENT_COLOR: Record<string, string> = {
  ADVISORY_DECISION: "text-sky-400",
  HITL_DECISION: "text-amber-400",
  MUTATION_TRAPPED: "text-rose-400",
  ROLLBACK_EXECUTED: "text-violet-400",
  SOP_PROMOTED: "text-emerald-400",
};

export const LEVEL_SHORT: Record<string, string> = {
  FULL_AUTO: "AUTO",
  SUGGEST_ONLY: "SUGG",
  HITL: "HITL",
  ALERT_ONLY: "ALRT",
};

export const LEVEL_COLOR: Record<string, string> = {
  FULL_AUTO: "text-emerald-400",
  SUGGEST_ONLY: "text-amber-400",
  HITL: "text-orange-400",
  ALERT_ONLY: "text-zinc-500",
};

export const PROBE_COLOR: Record<string, string> = {
  PASSED: "text-emerald-400",
  WARN: "text-amber-400",
  FAILED: "text-rose-400",
  INCONCLUSIVE: "text-zinc-600",
};

export const SEVERITIES = ["critical", "high", "medium", "low"] as const;
