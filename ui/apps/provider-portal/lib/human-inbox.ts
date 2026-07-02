import { backendGet } from "@aoip/api-client";
import type { ProviderHumanInboxResponse } from "@aoip/shared-types";
import { backendConfig } from "@/lib/config";

export type HumanInboxResult =
  | { status: "ok"; data: ProviderHumanInboxResponse }
  | { status: "error"; code: number };

export async function fetchHumanInbox(cookieHeader: string): Promise<HumanInboxResult> {
  let resp: Response;
  try {
    resp = await backendGet(backendConfig, "/human-inbox", cookieHeader);
  } catch {
    return { status: "error", code: 0 };
  }
  if (!resp.ok) return { status: "error", code: resp.status };
  return { status: "ok", data: (await resp.json()) as ProviderHumanInboxResponse };
}
