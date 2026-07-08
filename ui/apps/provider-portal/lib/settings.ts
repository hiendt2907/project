import { backendGet } from "@aoip/api-client";
import { backendConfig } from "@/lib/config";

export interface ProviderTenantSummary {
  tenant_id: string;
  display_name: string;
  status: string;
}

export interface ProviderAgentCredential {
  id: number;
  agent_id: string;
  hostname: string;
  key_prefix: string;
  status: "active" | "revoked" | string;
  created_at: string;
  revoked_at: string;
}

export interface ProviderSettingsResponse {
  tenants: ProviderTenantSummary[];
  agent_credentials: Record<string, ProviderAgentCredential[]>;
}

export type SettingsResult =
  | { status: "ok"; data: ProviderSettingsResponse }
  | { status: "error"; code: number };

export async function fetchSettings(cookieHeader: string): Promise<SettingsResult> {
  let resp: Response;
  try {
    resp = await backendGet(backendConfig, "/settings", cookieHeader);
  } catch {
    return { status: "error", code: 0 };
  }
  if (!resp.ok) return { status: "error", code: resp.status };
  return { status: "ok", data: (await resp.json()) as ProviderSettingsResponse };
}
