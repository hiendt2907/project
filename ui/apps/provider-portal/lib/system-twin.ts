import { fetchGatewaySection, type GatewaySectionResult } from "@/lib/gateway";

export interface SystemTwinResponse {
  tenant_id: string;
  revision: number;
  summary: {
    hosts: number;
    services: number;
    edges: number;
    unknown_edge_targets: string[];
    contradictions: number;
    unknowns: number;
  };
  entities: { hosts: string[]; services: string[] };
  operational_hosts: Array<{
    host: string;
    ports: string[];
    services: Array<{ name: string; ports: string[]; confidence: number; provenance: string[] }>;
    connections: Array<{ target: string; confidence: number; provenance: string[] }>;
  }>;
  api_sequence: {
    status: "runtime_verified" | "contract_observed" | "missing_contract" | "network_only";
    evidence: string;
    interactions: Array<{
      source_host: string;
      target_host: string | null;
      method: string;
      route: string;
      operation_id?: string;
      status_class: string;
      count: number;
      runtime_observed?: boolean;
      source_path: string;
      confidence: number;
      provenance: string;
    }>;
    unknown_reasons: string[];
  };
  edges: Array<{
    subject: string;
    predicate: string;
    object: string;
    confidence: number;
    provenance: string[];
  }>;
  unknowns: Array<Record<string, unknown>>;
  contradictions: Array<Record<string, unknown>>;
}

export function fetchSystemTwin(tenantId: string): Promise<GatewaySectionResult<SystemTwinResponse>> {
  return fetchGatewaySection<SystemTwinResponse>(
    `/onboarding/system-twin?tenant_id=${encodeURIComponent(tenantId)}`,
  );
}
