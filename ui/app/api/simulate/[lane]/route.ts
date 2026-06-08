import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

// Simulator proxy → POST /simulate/{lane}
// Injects a REAL synthetic alert for the lane into the live pipeline and returns
// the trace_id the UI then follows via /api/trace/{id}/pipeline + /api/trace/stream.
// When the gateway is not configured, returns a mock trace_id so the UI still
// renders (mock pipeline data flows through the existing trace proxy fallback).

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export const dynamic = "force-dynamic";

export const SIMULATOR_LANES = ["sys_resource", "sys_hard_fail", "app_http", "siem_security"] as const;
export type SimulatorLane = (typeof SIMULATOR_LANES)[number];

export interface SimulateResponse {
  status: string;
  lane: string;
  lane_label: string;
  trace_id: string;
  topic: string;
  ingress: "alert" | "evidence";
  source?: "gateway" | "mock";
}

const LANE_LABEL: Record<string, string> = {
  sys_resource: "SYS_RESOURCE",
  sys_hard_fail: "SYS_HARD_FAIL",
  app_http: "APP_HTTP",
  siem_security: "SIEM_SECURITY",
};

function mockResponse(lane: string): NextResponse {
  const id = `sim-${lane}-${Math.random().toString(16).slice(2, 14)}`;
  return NextResponse.json({
    status: "injected",
    lane,
    lane_label: LANE_LABEL[lane] ?? lane,
    trace_id: id,
    topic: "mock",
    ingress: lane === "sys_resource" || lane === "sys_hard_fail" ? "alert" : "evidence",
    source: "mock",
  } satisfies SimulateResponse);
}

export async function POST(
  _request: NextRequest,
  { params }: { params: Promise<{ lane: string }> }
) {
  const { lane } = await params;
  if (!SIMULATOR_LANES.includes(lane as SimulatorLane)) {
    return NextResponse.json({ error: `unknown lane '${lane}'` }, { status: 400 });
  }

  if (!GATEWAY_URL) {
    return mockResponse(lane);
  }

  try {
    const res = await fetch(`${GATEWAY_URL}/simulate/${encodeURIComponent(lane)}`, {
      method: "POST",
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
    });
    if (!res.ok) {
      return mockResponse(lane);
    }
    const data = (await res.json()) as SimulateResponse;
    return NextResponse.json({ ...data, source: "gateway" });
  } catch {
    return mockResponse(lane);
  }
}
