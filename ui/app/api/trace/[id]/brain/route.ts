import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

// Trace Redis second-brain proxy → GET /trace/{id}/brain
// Returns the multi-turn RAG session (turns, queries, hits, confidence) that fed
// the LLM. 404 → {found:false} (not an error).

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export const dynamic = "force-dynamic";

export interface BrainHit {
  score: number;
  point_id: string;
  collection: string;
  summary: string;
}

export interface BrainTurn {
  turn: number;
  query: string;
  top_score: number;
  hits: BrainHit[];
}

export interface TraceBrain {
  found: boolean;
  trace_id: string;
  source?: "gateway";
  top_score?: number;
  confident?: boolean;
  turn_count?: number;
  answer?: string;
  turns?: BrainTurn[];
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const traceId = decodeURIComponent(id);
  if (!GATEWAY_URL) return NextResponse.json({ found: false, trace_id: traceId });
  try {
    const res = await fetch(`${GATEWAY_URL}/trace/${encodeURIComponent(traceId)}/brain`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
    });
    if (res.status === 404) return NextResponse.json({ found: false, trace_id: traceId, source: "gateway" });
    if (!res.ok) return NextResponse.json({ found: false, trace_id: traceId }, { status: 502 });
    return NextResponse.json(await res.json());
  } catch {
    return NextResponse.json({ found: false, trace_id: traceId }, { status: 502 });
  }
}
