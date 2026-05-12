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

const MOCK_COMPONENTS: DeployComponent[] = [
  { name: "omni-analyst", role: "analyst", current_version: "v2.3.1-sprint5", status: "running", last_deployed: "2026-05-11T00:00:00Z", replicas: 2 },
  { name: "omni-prober", role: "prober", current_version: "v2.3.1-sprint5", status: "running", last_deployed: "2026-05-11T00:00:00Z", replicas: 1 },
  { name: "omni-executor", role: "executor", current_version: "v2.3.1-sprint5", status: "running", last_deployed: "2026-05-11T00:00:00Z", replicas: 1 },
  { name: "omni-core", role: "core", current_version: "v2.3.1-sprint5", status: "running", last_deployed: "2026-05-11T00:00:00Z", replicas: 1 },
  { name: "omni-gateway", role: "gateway", current_version: "v2.3.1-sprint5", status: "running", last_deployed: "2026-05-11T00:00:00Z", replicas: 2 },
  { name: "omni-siem-bridge", role: "siem-bridge", current_version: "v2.3.1-sprint5", status: "running", last_deployed: "2026-05-11T00:00:00Z", replicas: 1 },
  { name: "omni-hitl-dispatcher", role: "hitl-dispatcher", current_version: "v2.3.1-sprint5", status: "running", last_deployed: "2026-05-11T00:00:00Z", replicas: 1 },
];

function statusFromOverall(overall: string): ComponentStatus {
  if (overall === "ok") return "running";
  if (overall === "degraded") return "degraded";
  return "down";
}

export async function GET() {
  if (GATEWAY_URL) {
    try {
      const res = await fetch(`${GATEWAY_URL}/agents`, {
        headers: authHeaders(),
        cache: "no-store",
      });
      if (res.ok) {
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
      }
    } catch {
      // fall through to mock
    }
  }

  return NextResponse.json({
    components: MOCK_COMPONENTS,
    source: "mock",
    note: "Gateway unavailable — showing mock data",
  });
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
