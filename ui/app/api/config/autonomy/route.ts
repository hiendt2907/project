import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export type AutonomyLevel = "FULL_AUTO" | "HITL" | "SUGGEST_ONLY" | "ALERT_ONLY";

export interface PolicyRule {
  lane: string;
  severity: string;
  level: AutonomyLevel;
}

export interface PolicyChange {
  id: string;
  timestamp: string;
  operator: string;
  lane: string;
  severity: string;
  old_level: AutonomyLevel;
  new_level: AutonomyLevel;
}

export interface AutonomyPolicyResponse {
  rules: PolicyRule[];
  history: PolicyChange[];
  last_modified: string;
  modified_by: string;
  source: "gateway" | "error";
}

function gatewayError(detail: string) {
  return NextResponse.json({ source: "error", error: detail, rules: [], history: [] }, { status: 502 });
}

export async function GET() {
  if (!GATEWAY_URL) {
    return gatewayError("OMNI_GATEWAY_URL not configured");
  }
  try {
    const headers: HeadersInit = GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {};
    const res = await fetch(`${GATEWAY_URL}/autonomy/policy`, { headers, cache: "no-store" });
    if (!res.ok) return gatewayError(`gateway /autonomy/policy ${res.status}`);
    const data = await res.json();
    return NextResponse.json({ rules: [], history: [], ...data, source: "gateway" });
  } catch {
    return gatewayError("gateway /autonomy/policy unreachable");
  }
}

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  const { lane, severity, level } = body as { lane?: string; severity?: string; level?: AutonomyLevel };

  if (!lane || !severity || !level) {
    return NextResponse.json({ error: "lane, severity, level required" }, { status: 400 });
  }

  if (!GATEWAY_URL) {
    return NextResponse.json({ error: "OMNI_GATEWAY_URL not configured" }, { status: 502 });
  }

  try {
    const headers: HeadersInit = {
      "Content-Type": "application/json",
      ...(GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {}),
    };
    const res = await fetch(`${GATEWAY_URL}/autonomy/policy/rule`, {
      method: "POST",
      headers,
      body: JSON.stringify({ lane, severity, level }),
    });
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `Gateway unreachable: ${msg}` }, { status: 502 });
  }
}
