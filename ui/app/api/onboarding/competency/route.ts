import { type NextRequest, NextResponse } from "next/server";

// Competency passthrough route — proxies gateway /onboarding/competency for
// one entity. NO mock fallback: honest 502 when the gateway is unreachable.

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

function gatewayError(detail: string) {
  return NextResponse.json({ source: "error", error: detail }, { status: 502 });
}

export async function GET(request: NextRequest) {
  if (!GATEWAY_URL) return gatewayError("OMNI_GATEWAY_URL not configured");
  const params = request.nextUrl.searchParams;
  const entityType = params.get("entity_type");
  const entityId = params.get("entity_id");
  if (!entityType || !entityId) {
    return NextResponse.json({ error: "entity_type and entity_id are required" }, { status: 400 });
  }
  const search = new URLSearchParams({ entity_type: entityType, entity_id: entityId });
  const tenantId = params.get("tenant_id");
  if (tenantId) search.set("tenant_id", tenantId);
  try {
    const res = await fetch(`${GATEWAY_URL}/onboarding/competency?${search.toString()}`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) return gatewayError(`gateway /onboarding/competency ${res.status}`);
    return NextResponse.json({ ...(await res.json()), source: "gateway" });
  } catch {
    return gatewayError("gateway /onboarding/competency unreachable");
  }
}
