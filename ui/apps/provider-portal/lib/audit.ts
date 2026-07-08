import { backendGet } from "@aoip/api-client";
import { backendConfig } from "@/lib/config";

export interface ProviderAuditBlock {
  seq: number;
  event_type: string;
  trace_id: string;
  tenant_id: string;
  timestamp_utc: string;
  block_hash: string;
  signed: boolean;
}

export interface ProviderAuditResponse {
  total: number;
  signed: number;
  event_counts: Record<string, number>;
  blocks: ProviderAuditBlock[];
}

export type AuditResult =
  | { status: "ok"; data: ProviderAuditResponse }
  | { status: "error"; code: number };

export async function fetchAudit(cookieHeader: string): Promise<AuditResult> {
  let resp: Response;
  try {
    resp = await backendGet(backendConfig, "/audit", cookieHeader);
  } catch {
    return { status: "error", code: 0 };
  }
  if (!resp.ok) return { status: "error", code: resp.status };
  return { status: "ok", data: (await resp.json()) as ProviderAuditResponse };
}
