// Số liệu vận hành 24h — chiếu Redis KPI ZSET qua gateway /kpi/* (read-only).
// Nguồn: src/gateway/routes/kpi.py (rolling 24h).
import { fetchGatewaySection, type GatewaySectionResult } from "@/lib/gateway";

export interface KpiSummary {
  generated_at: string;
  window: string;
  advisory: {
    accepted: number;
    rejected: number;
    total: number;
    acceptance_rate: number | null;
  };
  execution: {
    total_executed: number;
    false_positive: number;
    false_positive_rate: number | null;
  };
}

export interface KpiTrend {
  window_seconds: number;
  lanes: Record<string, { detected: number; resolved: number }>;
}

export async function fetchKpiSummary(): Promise<GatewaySectionResult<KpiSummary>> {
  return fetchGatewaySection("/kpi/summary");
}

export async function fetchKpiTrend(): Promise<GatewaySectionResult<KpiTrend>> {
  return fetchGatewaySection("/kpi/trend?window=24h");
}

export function percentVI(rate: number | null): string {
  if (rate === null || Number.isNaN(rate)) return "chưa có dữ liệu";
  return `${(rate * 100).toFixed(1)}%`;
}
