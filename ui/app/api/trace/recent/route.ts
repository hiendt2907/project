import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { MOCK_RECENT_TRACES } from "@/mocks/pipeline-mock";

// Recent active traces — polling endpoint (chosen over SSE proxy for App Router robustness).
// SSE proxy note: Next.js App Router does not support long-lived SSE proxy cleanly
// (Edge runtime required; Node runtime buffers). Instead this polling endpoint returns
// recent active traces, refreshed every ~3s by the client. The pipeline page uses this
// to populate the trace list, then polls /api/trace/[id]/pipeline for the selected trace.
//
// When the gateway is available: GET {gateway}/trace/recent → list of RecentTrace.
// When unavailable: returns mock data.

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export const dynamic = "force-dynamic";

export interface RecentTrace {
  trace_id: string;
  lane: string;
  current_stage: string;
  verdict: string;
  started_at: number;
  updated_at: number;
}

export interface RecentTracesResponse {
  traces: RecentTrace[];
  source: "gateway" | "mock" | "error";
}

export async function GET(_request: NextRequest) {
  if (!GATEWAY_URL) {
    return NextResponse.json({
      traces: MOCK_RECENT_TRACES,
      source: "mock",
    } satisfies RecentTracesResponse);
  }

  try {
    const res = await fetch(`${GATEWAY_URL}/trace/recent`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
    });

    if (!res.ok) {
      return NextResponse.json({ traces: MOCK_RECENT_TRACES, source: "mock" } satisfies RecentTracesResponse);
    }

    const data = await res.json();
    const traces: RecentTrace[] = data.traces ?? data ?? [];
    return NextResponse.json({ traces, source: "gateway" } satisfies RecentTracesResponse);
  } catch {
    return NextResponse.json({ traces: MOCK_RECENT_TRACES, source: "mock" } satisfies RecentTracesResponse);
  }
}
