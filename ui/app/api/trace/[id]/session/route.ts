import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

// Trace diagnosis-session proxy → GET /trace/{id}/session
// Returns the sanitized multi-turn session (metadata-only previews).
// Falls back to a mock session when the gateway is unavailable.

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export const dynamic = "force-dynamic";

export interface CommandPreview {
  head: string[];
  tail: string[];
  total_lines: number;
  truncated: boolean;
}

export interface CommandResult {
  cmd_id: string;
  command_str: string;
  purpose: string;
  rc: number;
  status: string;
  blocked: boolean;
  block_reason: string;
  preview: CommandPreview;
  stderr_preview: string;
}

export interface DiagnosisTurn {
  turn: number;
  reasoning: string;
  hypothesis: string;
  evidence_gaps: string[];
  confidence: number;
  commands_requested: { command: string; args?: string[]; purpose?: string }[];
  command_results: CommandResult[];
  diagnosis_complete_claimed: boolean;
}

export interface TraceSession {
  found: boolean;
  source: "gateway" | "error";
  trace_id: string;
  agent_id: string;
  probe: string;
  lane: string;
  alert_hint: string;
  total_turns: number;
  degraded: boolean;
  degraded_reason: string;
  completed_at: number;
  turns: DiagnosisTurn[];
  final: {
    root_cause: string;
    affected_components: string[];
    blast_radius: string;
    impact_summary: string;
    remediation_steps: string[];
    confidence: number;
  };
}

function gatewayError(traceId: string, detail: string) {
  return NextResponse.json(
    { found: false, source: "error", error: detail, trace_id: traceId },
    { status: 502 }
  );
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const traceId = decodeURIComponent(id);
  if (!GATEWAY_URL) {
    return gatewayError(traceId, "OMNI_GATEWAY_URL not configured");
  }
  try {
    const res = await fetch(`${GATEWAY_URL}/trace/${encodeURIComponent(traceId)}/session`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
    });
    if (res.status === 404) {
      // Honest "no session stored" — not an error, just empty.
      return NextResponse.json({ found: false, source: "gateway", trace_id: traceId });
    }
    if (!res.ok) return gatewayError(traceId, `gateway /trace ${res.status}`);
    const data = (await res.json()) as TraceSession;
    return NextResponse.json(data);
  } catch {
    return gatewayError(traceId, "gateway /trace unreachable");
  }
}
