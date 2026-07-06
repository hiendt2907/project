import { fetchGatewaySection, type GatewaySectionResult } from "@/lib/gateway";

export interface ReadinessRecord {
  endpoint_mapped_pct: number | null;
  business_flow_confirmed_pct: number | null;
  open_questions_over_threshold: number;
  readiness_flag: boolean;
  updated_at: string | null;
}

export interface ReadinessThresholds {
  endpoint_mapped_pct_min: number;
  business_flow_confirmed_pct_min: number;
  open_questions_max: number;
  open_question_stale_days: number;
}

export interface ReadinessResponse {
  tenant_id: string;
  readiness: ReadinessRecord | null;
  thresholds: ReadinessThresholds;
}

export function fetchReadiness(tenantId: string): Promise<GatewaySectionResult<ReadinessResponse>> {
  return fetchGatewaySection<ReadinessResponse>(`/onboarding/readiness?tenant_id=${encodeURIComponent(tenantId)}`);
}
