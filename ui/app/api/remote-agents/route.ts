import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

function authHeaders(): HeadersInit {
  return GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {};
}

export interface AgentMetrics {
  cpu_percent: number;
  mem_percent: number;
  mem_used_mb: number;
  disk_percent: number;
  load_avg_1m: number;
}

export interface RemoteAgent {
  agent_id: string;
  hostname: string;
  version: string;
  capabilities: string[];
  platform: string;
  k8s_namespace: string;
  registered_at: number;
  last_seen: number;
  age_seconds: number;
  online: boolean;
  status: "online" | "offline";
  evidence_count?: number;
  metrics?: AgentMetrics | null;
  eps?: number;
  type: "remote";
}

export interface RemoteAgentsResponse {
  generated_at: string;
  count: number;
  online: number;
  agents: RemoteAgent[];
  source: "gateway" | "mock" | "error";
}

function buildMock(): RemoteAgentsResponse {
  const now = Math.floor(Date.now() / 1000);
  return {
    generated_at: new Date().toISOString(),
    count: 3,
    online: 3,
    source: "mock",
    agents: [
      {
        agent_id: "loyalty-uat",
        hostname: "zabbix-uat",
        version: "1.0.0",
        capabilities: ["metrics", "logs", "database", "services", "storage"],
        platform: "linux",
        k8s_namespace: "",
        registered_at: now - 3600,
        last_seen: now - 5,
        age_seconds: 5,
        online: true,
        status: "online",
        metrics: { cpu_percent: 45.2, mem_percent: 72.3, mem_used_mb: 7834, disk_percent: 65.0, load_avg_1m: 1.8 },
        eps: 0.1,
        type: "remote",
      },
      {
        agent_id: "uat-proxysql",
        hostname: "uat-proxysql",
        version: "1.0.0",
        capabilities: ["metrics", "logs", "database", "services", "storage"],
        platform: "linux",
        k8s_namespace: "",
        registered_at: now - 1800,
        last_seen: now - 12,
        age_seconds: 12,
        online: true,
        status: "online",
        metrics: { cpu_percent: 28.0, mem_percent: 58.4, mem_used_mb: 4800, disk_percent: 42.0, load_avg_1m: 0.9 },
        eps: 0.1,
        type: "remote",
      },
      {
        agent_id: "uat-proxysql2",
        hostname: "uat-proxysql2",
        version: "1.0.0",
        capabilities: ["metrics", "logs", "database", "services", "storage"],
        platform: "linux",
        k8s_namespace: "",
        registered_at: now - 1200,
        last_seen: now - 8,
        age_seconds: 8,
        online: true,
        status: "online",
        metrics: { cpu_percent: 12.5, mem_percent: 41.0, mem_used_mb: 3276, disk_percent: 91.0, load_avg_1m: 0.4 },
        eps: 0.1,
        type: "remote",
      },
    ],
  };
}

export async function GET() {
  if (!GATEWAY_URL) {
    return NextResponse.json(buildMock());
  }
  try {
    const res = await fetch(`${GATEWAY_URL}/agents/remote`, {
      headers: authHeaders(),
      cache: "no-store",
    });
    if (!res.ok) return NextResponse.json({ source: "error" } as Partial<RemoteAgentsResponse>, { status: 502 });
    const data = await res.json();
    return NextResponse.json({ ...data, source: "gateway" } as RemoteAgentsResponse);
  } catch {
    return NextResponse.json({ source: "error" } as Partial<RemoteAgentsResponse>, { status: 502 });
  }
}
