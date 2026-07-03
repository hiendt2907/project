import { type NextRequest, NextResponse } from "next/server";

// Diagram history passthrough route — proxies gateway /onboarding/diagram/history
// (newest-first, anchored at latest). NO mock fallback: honest 502 when the
// gateway is unreachable. Params are validated before forwarding.

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

function gatewayError(detail: string) {
  return NextResponse.json({ source: "error", error: detail }, { status: 502 });
}

function parseBoundedInt(raw: string | null, min: number, max: number): number | null | "invalid" {
  if (raw === null) return null;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < min || n > max) return "invalid";
  return n;
}

export async function GET(request: NextRequest) {
  if (!GATEWAY_URL) return gatewayError("OMNI_GATEWAY_URL not configured");
  const params = request.nextUrl.searchParams;
  const limit = parseBoundedInt(params.get("limit"), 1, 50);
  const before = parseBoundedInt(params.get("before"), 2, Number.MAX_SAFE_INTEGER);
  if (limit === "invalid" || before === "invalid") {
    return NextResponse.json({ error: "limit must be 1-50; before must be an integer >= 2" }, { status: 400 });
  }
  const search = new URLSearchParams();
  if (limit !== null) search.set("limit", String(limit));
  if (before !== null) search.set("before", String(before));
  const tenantId = params.get("tenant_id");
  if (tenantId) search.set("tenant_id", tenantId);
  try {
    const res = await fetch(`${GATEWAY_URL}/onboarding/diagram/history?${search.toString()}`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) return gatewayError(`gateway /onboarding/diagram/history ${res.status}`);
    return NextResponse.json({ ...(await res.json()), source: "gateway" });
  } catch {
    return gatewayError("gateway /onboarding/diagram/history unreachable");
  }
}
