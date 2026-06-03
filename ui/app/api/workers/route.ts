import { type NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export type WorkerStatus = "healthy" | "degraded" | "unhealthy" | "unknown";

export interface WorkerDetail {
  role: string;
  status: WorkerStatus;
  replicas: number;
  ready: number;
  last_heartbeat_age_seconds: number;
  last_message_type: string;
  error_count_24h: number;
  description: string;
}

export interface WorkersResponse {
  workers: WorkerDetail[];
  overall: WorkerStatus;
  healthy_count: number;
  degraded_count: number;
  unhealthy_count: number;
  source: "gateway";
  generated_at: string;
}

const ROLE_DESCRIPTIONS: Record<string, string> = {
  prober: "Kafka alerts loop + proactive PromQL + circuit breaker",
  analyst: "Kafka evidence loop + LLM advisory + KPI collector",
  core: "Deep scout + forecast + baseline snapshot + proactive",
  executor: "Kafka actions loop — mutation executor",
  gateway: "FastAPI HTTP gateway → Kafka omni-alerts",
  "siem-bridge": "Redis XREADGROUP → Kafka omni-alerts",
  "evidence-adapter": "Redis XREADGROUP → Kafka omni-diagnostic-evidence",
  "hitl-dispatcher": "omni-hitl-pending → FinGuard HITL API",
};

function deriveStatus(w: { ready: number; replicas: number; last_heartbeat_age_seconds: number }): WorkerStatus {
  if (w.last_heartbeat_age_seconds > 300) return "unhealthy";
  if (w.ready < w.replicas || w.last_heartbeat_age_seconds > 90) return "degraded";
  return "healthy";
}

function gatewayError(detail: string) {
  return NextResponse.json({ source: "error", error: detail }, { status: 502 });
}

export async function GET(request: NextRequest) {
  const tenantId = request.nextUrl.searchParams.get("tenant_id");
  if (!GATEWAY_URL) {
    return gatewayError("OMNI_GATEWAY_URL not configured");
  }

  const tenantParam = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : "";
  try {
    const res = await fetch(`${GATEWAY_URL}/agents${tenantParam}`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
    });

    if (!res.ok) return gatewayError(`gateway /agents ${res.status}`);

    const data = await res.json();
    const agentList: { role: string; replicas: number; ready: number; last_heartbeat_age_sec: number }[] =
      data.agents ?? data.workers ?? [];

    const workers: WorkerDetail[] = agentList.map((a) => {
      const ageSeconds = a.last_heartbeat_age_sec ?? 0;
      const w = { ready: a.ready, replicas: a.replicas, last_heartbeat_age_seconds: ageSeconds };
      return {
        role: a.role,
        replicas: a.replicas,
        ready: a.ready,
        last_heartbeat_age_seconds: ageSeconds,
        last_message_type: "heartbeat",
        error_count_24h: 0,
        status: deriveStatus(w),
        description: ROLE_DESCRIPTIONS[a.role] ?? "Worker process",
      };
    });

    const healthy_count = workers.filter((w) => w.status === "healthy").length;
    const degraded_count = workers.filter((w) => w.status === "degraded").length;
    const unhealthy_count = workers.filter((w) => w.status === "unhealthy").length;

    let overall: WorkerStatus = "healthy";
    if (unhealthy_count > 0) overall = "unhealthy";
    else if (degraded_count > 0) overall = "degraded";

    return NextResponse.json({
      workers,
      overall,
      healthy_count,
      degraded_count,
      unhealthy_count,
      source: "gateway",
      generated_at: new Date().toISOString(),
    } satisfies WorkersResponse);
  } catch {
    return gatewayError("gateway /agents unreachable");
  }
}
