import { backendGet } from "@aoip/api-client";
import { backendConfig } from "@/lib/config";

export type TenantMission = {
  mission_id: string; goal: string; scope: string; state: string;
  completion: number; updated_at: number; next_action?: string; last_activity?: string;
};

export async function fetchMissions(cookieHeader: string): Promise<TenantMission[] | null> {
  try {
    const resp = await backendGet(backendConfig, "/missions", cookieHeader);
    if (!resp.ok) return null;
    return ((await resp.json()) as { missions?: TenantMission[] }).missions ?? [];
  } catch { return null; }
}
