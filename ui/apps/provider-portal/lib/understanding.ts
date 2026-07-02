import { backendGet } from "@aoip/api-client";
import type { ProviderUnderstandingResponse } from "@aoip/shared-types";
import { backendConfig } from "@/lib/config";

export type UnderstandingResult =
  | { status: "ok"; data: ProviderUnderstandingResponse }
  | { status: "error"; code: number };

export async function fetchUnderstanding(cookieHeader: string): Promise<UnderstandingResult> {
  let resp: Response;
  try {
    resp = await backendGet(backendConfig, "/understanding", cookieHeader);
  } catch {
    return { status: "error", code: 0 };
  }
  if (!resp.ok) return { status: "error", code: resp.status };
  return { status: "ok", data: (await resp.json()) as ProviderUnderstandingResponse };
}
