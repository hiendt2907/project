import { fetchGatewaySection, type GatewaySectionResult } from "@/lib/gateway";

export interface MutationToggleResponse {
  tenant_id: string;
  requested: boolean;
  master_kill_switch: boolean;
  effective: boolean;
  reason: "tenant_toggle_off" | "master_kill_switch_off" | "enabled" | string;
  flag_key: string;
}

export function fetchMutationToggle(tenantId: string): Promise<GatewaySectionResult<MutationToggleResponse>> {
  return fetchGatewaySection<MutationToggleResponse>(
    `/autonomy/mutation?tenant_id=${encodeURIComponent(tenantId)}`,
  );
}
