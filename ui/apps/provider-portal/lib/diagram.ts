import { fetchGatewaySection, type GatewaySectionResult } from "@/lib/gateway";

export interface DiagramResponse {
  tenant_id: string;
  version: number | null;
  mermaid: string | null;
}

export function fetchDiagram(tenantId: string): Promise<GatewaySectionResult<DiagramResponse>> {
  return fetchGatewaySection<DiagramResponse>(`/onboarding/diagram?tenant_id=${encodeURIComponent(tenantId)}`);
}
