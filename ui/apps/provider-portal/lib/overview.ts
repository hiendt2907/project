import { backendGet } from "@aoip/api-client";
import type { ProviderOverview } from "@aoip/shared-types";
import { backendConfig } from "@/lib/config";

export type OverviewResult =
  | { status: "ok"; overview: ProviderOverview }
  | { status: "error"; code: number };

/** Lấy Overview control-tower SERVER-SIDE (chuyển tiếp cookie phiên). Backend enforce RBAC. */
export async function fetchOverview(cookieHeader: string): Promise<OverviewResult> {
  let resp: Response;
  try {
    resp = await backendGet(backendConfig, "/overview", cookieHeader);
  } catch {
    return { status: "error", code: 0 };
  }
  if (!resp.ok) return { status: "error", code: resp.status };
  return { status: "ok", overview: (await resp.json()) as ProviderOverview };
}
