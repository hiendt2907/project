import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import {
  MOCK_PIPELINE_TRACES,
  MOCK_PIPELINE_SIEM,
} from "@/mocks/pipeline-mock";

// Trace pipeline proxy → GET /trace/{id}/pipeline
// Returns 11-stage pipeline status for a trace.
// Falls back to mock data when gateway is unavailable.

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export const dynamic = "force-dynamic";

export type PipelineStageStatus = "ok" | "fail" | "skip" | "pending";
export type PipelineStage =
  | "INGEST"
  | "EVIDENCE"
  | "RAG"
  | "LLM"
  | "VERIFY"
  | "SCHEMA"
  | "KILLSWITCH"
  | "CRAT"
  | "DISPATCH"
  | "HITL"
  | "EXECUTOR"
  | "FEEDBACK";

export interface PipelineStageEntry {
  stage: PipelineStage;
  status: PipelineStageStatus;
  ts: number;
  detail: string;
  elapsed_ms: number;
}

export interface PipelineResponse {
  found: boolean;
  trace_id: string;
  lane: string;
  started_at: number;
  updated_at: number;
  verdict: string;
  stages: PipelineStageEntry[];
  source?: "gateway" | "mock" | "error";
}

function mockFallback(traceId: string): NextResponse {
  const match = MOCK_PIPELINE_TRACES.find((t) => t.trace_id === traceId);
  const data = match ?? MOCK_PIPELINE_SIEM;
  return NextResponse.json({ ...data, source: "mock" } satisfies PipelineResponse);
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const traceId = decodeURIComponent(id);

  if (!GATEWAY_URL) {
    return mockFallback(traceId);
  }

  try {
    const res = await fetch(
      `${GATEWAY_URL}/trace/${encodeURIComponent(traceId)}/pipeline`,
      {
        headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
        cache: "no-store",
      }
    );

    if (res.status === 404) {
      return NextResponse.json(
        { found: false, source: "gateway", trace_id: traceId } as Partial<PipelineResponse>,
        { status: 404 }
      );
    }
    if (!res.ok) {
      return mockFallback(traceId);
    }

    const data = (await res.json()) as PipelineResponse;
    return NextResponse.json({ ...data, source: "gateway" });
  } catch {
    return mockFallback(traceId);
  }
}
