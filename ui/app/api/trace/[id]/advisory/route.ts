import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

// Trace advisory proxy → GET /trace/{id}/advisory
// Returns the stored AnalystAdvisory (verification_steps, impact_chain, remediation,
// forecast) for a single-pass advisory trace. 404 → {found:false} (not an error).

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export const dynamic = "force-dynamic";

export interface VerificationStep {
  order: number;
  layer: string;
  command: string;
  expected_output: string;
  rationale: string;
}

export interface RemediationStep {
  order: number;
  action: string;
  approval_required: boolean;
  rollback_plan: string;
}

export interface ImpactForecast {
  timeframe: string;
  severity: string;
  prediction: string;
  confidence: string;
}

export interface ImpactChainLink {
  cause: string;
  mechanism: string;
  effect: string;
  evidence_lane: string;
  confidence: string;
}

export interface TraceAdvisory {
  found: boolean;
  trace_id: string;
  lane?: string;
  source?: "gateway";
  advisory?: {
    verdict: string;
    root_cause: string;
    confidence: string;
    affected_workload: string;
    verification_steps: VerificationStep[];
    proposed_remediation: RemediationStep[];
    forecast?: { method: string; basis: string; forecasts: ImpactForecast[] };
    impact_chain?: ImpactChainLink[];
  };
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const traceId = decodeURIComponent(id);
  if (!GATEWAY_URL) {
    return NextResponse.json({ found: false, trace_id: traceId });
  }
  try {
    const res = await fetch(`${GATEWAY_URL}/trace/${encodeURIComponent(traceId)}/advisory`, {
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
