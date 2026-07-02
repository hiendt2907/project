import { backendGet } from "@aoip/api-client";
import type { ProviderAgentsResponse } from "@aoip/shared-types";
import { backendConfig } from "@/lib/config";

export type AgentsResult =
  | { status: "ok"; data: ProviderAgentsResponse }
  | { status: "error"; code: number };

export async function fetchAgents(cookieHeader: string): Promise<AgentsResult> {
  let resp: Response;
  try {
    resp = await backendGet(backendConfig, "/agents", cookieHeader);
  } catch {
    return { status: "error", code: 0 };
  }
  if (!resp.ok) return { status: "error", code: resp.status };
  return { status: "ok", data: (await resp.json()) as ProviderAgentsResponse };
}
