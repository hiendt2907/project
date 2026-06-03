import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export interface SiemCategoryDistribution {
  ddos: number;
  malware: number;
  data_exfil: number;
  k8s_threat: number;
  auth_failure: number;
  lateral_movement: number;
  network_anomaly: number;
}

export interface SiemRecentIncident {
  id: string;
  timestamp: string;
  category: keyof SiemCategoryDistribution;
  severity: "critical" | "high" | "medium" | "low";
  tenant: string;
  source_ip: string;
  status: "ACTIVE" | "HITL_PENDING" | "RESOLVED";
}

export interface SiemOpsResponse {
  total_24h: number;
  critical_count: number;
  hitl_pending: number;
  resolved_24h: number;
  category_distribution: SiemCategoryDistribution;
  recent_incidents: SiemRecentIncident[];
  kill_chain_active_stages: string[];
  source: "gateway";
  generated_at: string;
}

function emptyDist(): SiemCategoryDistribution {
  return { ddos: 0, malware: 0, data_exfil: 0, k8s_threat: 0, auth_failure: 0, lateral_movement: 0, network_anomaly: 0 };
}

function gatewayError(detail: string) {
  return NextResponse.json({ source: "error", error: detail }, { status: 502 });
}

export async function GET() {
  if (!GATEWAY_URL) {
    return gatewayError("OMNI_GATEWAY_URL not configured");
  }

  try {
    const headers: HeadersInit = GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {};
    const res = await fetch(`${GATEWAY_URL}/siem/overview?limit=50`, { headers, cache: "no-store" });
    if (!res.ok) return gatewayError(`gateway /siem/overview ${res.status}`);

    const data = await res.json();
    const byCategory = data.ingestion?.by_category ?? [];
    const dist = emptyDist();
    for (const entry of byCategory) {
      const key = entry.name as keyof SiemCategoryDistribution;
      if (key in dist) dist[key] = entry.count;
    }

    return NextResponse.json({
      total_24h: data.ingestion?.total_last_24h ?? 0,
      critical_count: data.ingestion?.by_severity?.critical ?? 0,
      hitl_pending: data.hitl?.pending ?? 0,
      resolved_24h: data.hitl?.approved_24h ?? 0,
      category_distribution: dist,
      recent_incidents: (data.recent_incidents ?? []) as SiemRecentIncident[],
      kill_chain_active_stages: (data.kill_chain_active_stages ?? []) as string[],
      source: "gateway",
      generated_at: new Date().toISOString(),
    } satisfies SiemOpsResponse);
  } catch {
    return gatewayError("gateway /siem/overview unreachable");
  }
}
