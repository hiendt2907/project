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
  source: "gateway" | "mock";
  generated_at: string;
}

function buildMock(): SiemOpsResponse {
  const now = Date.now();
  return {
    total_24h: 362,
    critical_count: 38,
    hitl_pending: 3,
    resolved_24h: 314,
    category_distribution: {
      ddos: 28,
      malware: 54,
      data_exfil: 42,
      k8s_threat: 71,
      auth_failure: 88,
      lateral_movement: 39,
      network_anomaly: 40,
    },
    recent_incidents: [
      {
        id: "fg-inc-a4b201",
        timestamp: new Date(now - 420_000).toISOString(),
        category: "k8s_threat",
        severity: "critical",
        tenant: "tenant-acme",
        source_ip: "10.244.1.42",
        status: "HITL_PENDING",
      },
      {
        id: "fg-inc-c81d44",
        timestamp: new Date(now - 1_200_000).toISOString(),
        category: "data_exfil",
        severity: "critical",
        tenant: "tenant-globex",
        source_ip: "203.0.113.88",
        status: "HITL_PENDING",
      },
      {
        id: "fg-inc-ee9c03",
        timestamp: new Date(now - 2_400_000).toISOString(),
        category: "lateral_movement",
        severity: "high",
        tenant: "tenant-acme",
        source_ip: "10.244.2.15",
        status: "ACTIVE",
      },
      {
        id: "fg-inc-bb3311",
        timestamp: new Date(now - 3_600_000).toISOString(),
        category: "malware",
        severity: "high",
        tenant: "tenant-initech",
        source_ip: "198.51.100.22",
        status: "RESOLVED",
      },
      {
        id: "fg-inc-dd7788",
        timestamp: new Date(now - 5_400_000).toISOString(),
        category: "auth_failure",
        severity: "medium",
        tenant: "tenant-umbrella",
        source_ip: "192.0.2.100",
        status: "RESOLVED",
      },
      {
        id: "fg-inc-ff2200",
        timestamp: new Date(now - 7_200_000).toISOString(),
        category: "ddos",
        severity: "critical",
        tenant: "tenant-globex",
        source_ip: "172.16.0.55",
        status: "RESOLVED",
      },
      {
        id: "fg-inc-aa5500",
        timestamp: new Date(now - 9_000_000).toISOString(),
        category: "network_anomaly",
        severity: "medium",
        tenant: "tenant-acme",
        source_ip: "10.244.3.7",
        status: "RESOLVED",
      },
    ],
    kill_chain_active_stages: ["initial_access", "execution", "lateral_movement"],
    source: "mock",
    generated_at: new Date().toISOString(),
  };
}

export async function GET() {
  if (!GATEWAY_URL) {
    return NextResponse.json(buildMock());
  }

  try {
    const headers: HeadersInit = GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {};
    const res = await fetch(`${GATEWAY_URL}/siem/overview?limit=50`, {
      headers,
      cache: "no-store",
    });

    if (!res.ok) {
      return NextResponse.json(buildMock());
    }

    const data = await res.json();
    const byCategory = data.ingestion?.by_category ?? [];

    const dist: SiemCategoryDistribution = {
      ddos: 0,
      malware: 0,
      data_exfil: 0,
      k8s_threat: 0,
      auth_failure: 0,
      lateral_movement: 0,
      network_anomaly: 0,
    };

    for (const entry of byCategory) {
      const key = entry.name as keyof SiemCategoryDistribution;
      if (key in dist) dist[key] = entry.count;
    }

    const mock = buildMock();
    return NextResponse.json({
      ...mock,
      total_24h: data.ingestion?.total_last_24h ?? mock.total_24h,
      critical_count: data.ingestion?.by_severity?.critical ?? mock.critical_count,
      hitl_pending: data.hitl?.pending ?? mock.hitl_pending,
      category_distribution: dist,
      source: "gateway",
      generated_at: new Date().toISOString(),
    } satisfies SiemOpsResponse);
  } catch {
    return NextResponse.json(buildMock());
  }
}
