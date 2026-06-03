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
  source: "gateway" | "error";
}

function gatewayError(detail: string) {
  return NextResponse.json({ source: "error", error: detail, agents: [], count: 0, online: 0 }, { status: 502 });
}

export async function GET() {
  if (!GATEWAY_URL) {
    return gatewayError("OMNI_GATEWAY_URL not configured");
  }
  try {
    const res = await fetch(`${GATEWAY_URL}/agents/remote`, { headers: authHeaders(), cache: "no-store" });
    if (!res.ok) return gatewayError(`gateway /agents/remote ${res.status}`);
    const data = await res.json();
    return NextResponse.json({ ...data, source: "gateway" } as RemoteAgentsResponse);
  } catch {
    return gatewayError("gateway /agents/remote unreachable");
  }
}
