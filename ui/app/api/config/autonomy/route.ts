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
  source: "gateway" | "mock";
}

const DEFAULT_RULES: PolicyRule[] = [
  { lane: "SYS_RESOURCE", severity: "critical", level: "HITL" },
  { lane: "SYS_RESOURCE", severity: "high", level: "SUGGEST_ONLY" },
  { lane: "SYS_RESOURCE", severity: "medium", level: "FULL_AUTO" },
  { lane: "SYS_RESOURCE", severity: "low", level: "FULL_AUTO" },
  { lane: "SYS_HARD_FAIL", severity: "critical", level: "HITL" },
  { lane: "SYS_HARD_FAIL", severity: "high", level: "SUGGEST_ONLY" },
  { lane: "SYS_HARD_FAIL", severity: "medium", level: "FULL_AUTO" },
  { lane: "SYS_HARD_FAIL", severity: "low", level: "FULL_AUTO" },
  { lane: "APP_HTTP", severity: "critical", level: "HITL" },
  { lane: "APP_HTTP", severity: "high", level: "SUGGEST_ONLY" },
  { lane: "APP_HTTP", severity: "medium", level: "SUGGEST_ONLY" },
  { lane: "APP_HTTP", severity: "low", level: "ALERT_ONLY" },
  { lane: "SIEM_SECURITY", severity: "critical", level: "HITL" },
  { lane: "SIEM_SECURITY", severity: "high", level: "HITL" },
  { lane: "SIEM_SECURITY", severity: "medium", level: "SUGGEST_ONLY" },
  { lane: "SIEM_SECURITY", severity: "low", level: "ALERT_ONLY" },
];

const MOCK_HISTORY: PolicyChange[] = [
  {
    id: "chg-001",
    timestamp: new Date(Date.now() - 86_400_000).toISOString(),
    operator: "admin@sre",
    lane: "SIEM_SECURITY",
    severity: "critical",
    old_level: "SUGGEST_ONLY",
    new_level: "HITL",
  },
  {
    id: "chg-002",
    timestamp: new Date(Date.now() - 172_800_000).toISOString(),
    operator: "noc@sre",
    lane: "SYS_RESOURCE",
    severity: "medium",
    old_level: "HITL",
    new_level: "FULL_AUTO",
  },
  {
    id: "chg-003",
    timestamp: new Date(Date.now() - 259_200_000).toISOString(),
    operator: "admin@sre",
    lane: "APP_HTTP",
    severity: "high",
    old_level: "FULL_AUTO",
    new_level: "SUGGEST_ONLY",
  },
];

function buildMock(): AutonomyPolicyResponse {
  return {
    rules: DEFAULT_RULES,
    history: MOCK_HISTORY,
    last_modified: new Date(Date.now() - 86_400_000).toISOString(),
    modified_by: "admin@sre",
    source: "mock",
  };
}

export async function GET() {
  if (!GATEWAY_URL) {
    return NextResponse.json(buildMock());
  }

  try {
    const headers: HeadersInit = GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {};
    const res = await fetch(`${GATEWAY_URL}/autonomy/policy`, { headers, cache: "no-store" });
    if (!res.ok) return NextResponse.json(buildMock());
    const data = await res.json();
    return NextResponse.json({ ...buildMock(), ...data, source: "gateway" });
  } catch {
    return NextResponse.json(buildMock());
  }
}

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  const { lane, severity, level } = body as { lane?: string; severity?: string; level?: AutonomyLevel };

  if (!lane || !severity || !level) {
    return NextResponse.json({ error: "lane, severity, level required" }, { status: 400 });
  }

  if (!GATEWAY_URL) {
    // Mock success
    return NextResponse.json({ success: true, rule: { lane, severity, level } });
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
