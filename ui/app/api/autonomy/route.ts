import { type NextRequest, NextResponse } from "next/server";

// Autonomy tier proxy → Omni Gateway /autonomy/tier + /autonomy/readiness.
// GET: current tier + readiness snapshot. POST: change tier (2-step confirm on promotion).

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

type Tier = "shadow" | "assist" | "auto";

interface TierReadiness {
  current_tier: Tier;
  next_tier: Tier | null;
  ready: boolean;
  elapsed_days: number;
  accepted: number;
  rejected: number;
  false_positive: number;
  total: number;
  wilson_lb: number;
  false_positive_rate: number;
  reasons: string[];
}

function authHeaders(): HeadersInit {
  return GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {};
}

function gatewayError(detail: string, status = 502) {
  return NextResponse.json({ source: "error", error: detail }, { status });
}

export async function GET(request: NextRequest) {
  const tenantId = request.nextUrl.searchParams.get("tenant_id") ?? "default";
  if (!GATEWAY_URL) return gatewayError("OMNI_GATEWAY_URL not configured");
  const q = `tenant_id=${encodeURIComponent(tenantId)}`;
  try {
    const [tierRes, readyRes] = await Promise.all([
      fetch(`${GATEWAY_URL}/autonomy/tier?${q}`, { headers: authHeaders(), cache: "no-store" }),
      fetch(`${GATEWAY_URL}/autonomy/readiness?${q}`, { headers: authHeaders(), cache: "no-store" }),
    ]);
    if (!tierRes.ok) return gatewayError(`gateway /autonomy/tier ${tierRes.status}`);
    const tierData = (await tierRes.json()) as { tier: Tier };
    const readyData = readyRes.ok ? ((await readyRes.json()) as { readiness: TierReadiness | null }) : { readiness: null };
    return NextResponse.json({ source: "gateway", tier: tierData.tier, readiness: readyData.readiness });
  } catch {
    return gatewayError("gateway /autonomy unreachable");
  }
}

export async function POST(request: NextRequest) {
  if (!GATEWAY_URL) return gatewayError("OMNI_GATEWAY_URL not configured");
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return gatewayError("invalid JSON body", 400);
  }
  try {
    const res = await fetch(`${GATEWAY_URL}/autonomy/tier`, {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      return NextResponse.json({ source: "error", ...data }, { status: res.status });
    }
    return NextResponse.json({ source: "gateway", ...data });
  } catch {
    return gatewayError("gateway /autonomy/tier POST unreachable");
  }
}
