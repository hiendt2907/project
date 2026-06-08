import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

// Trace per-phase logs proxy → GET /trace/{id}/logs
// Returns the raw log stream {ts, phase, level, line} (newest last).

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export const dynamic = "force-dynamic";

export interface TraceLogEntry {
  ts: number;
  phase: string;
  level: string;
  line: string;
}

export interface TraceLogs {
  trace_id: string;
  logs: TraceLogEntry[];
  source?: string;
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const traceId = decodeURIComponent(id);
  if (!GATEWAY_URL) return NextResponse.json({ trace_id: traceId, logs: [], source: "no-gateway" });
  try {
    const res = await fetch(`${GATEWAY_URL}/trace/${encodeURIComponent(traceId)}/logs`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
    });
    if (!res.ok) return NextResponse.json({ trace_id: traceId, logs: [], source: "error" });
    return NextResponse.json(await res.json());
  } catch {
    return NextResponse.json({ trace_id: traceId, logs: [], source: "error" });
  }
}
