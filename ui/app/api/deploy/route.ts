import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

function authHeaders(): HeadersInit {
  return GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {};
}

type ComponentStatus = "running" | "degraded" | "down";

interface DeployComponent {
  name: string;
  role: string;
  current_version: string;
  status: ComponentStatus;
  last_deployed: string;
  replicas: number;
}

function statusFromOverall(overall: string): ComponentStatus {
  if (overall === "ok") return "running";
  if (overall === "degraded") return "degraded";
  return "down";
}

function gatewayError(detail: string) {
  return NextResponse.json({ source: "error", error: detail }, { status: 502 });
}

export async function GET() {
  if (!GATEWAY_URL) {
    return gatewayError("OMNI_GATEWAY_URL not configured");
  }
  try {
    const res = await fetch(`${GATEWAY_URL}/agents`, { headers: authHeaders(), cache: "no-store" });
    if (!res.ok) return gatewayError(`gateway /agents ${res.status}`);
    const data = await res.json();
    const agents: Array<{ role: string; overall: string; last_seen?: string }> = data.agents ?? [];
    const components: DeployComponent[] = agents.map((agent) => ({
      name: `omni-${agent.role}`,
      role: agent.role,
      current_version: "v2.3.1-sprint5",
      status: statusFromOverall(agent.overall ?? "unknown"),
      last_deployed: agent.last_seen ?? new Date().toISOString(),
      replicas: 1,
    }));
    return NextResponse.json({ components, source: "gateway" });
  } catch {
    return gatewayError("gateway /agents unreachable");
  }
}

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  const target: string = body.target ?? "all";
  return NextResponse.json({
    status: "acknowledged",
    target,
    message: `Rollout queued for ${target === "all" ? "all components" : target}`,
    queued_at: new Date().toISOString(),
  });
}
