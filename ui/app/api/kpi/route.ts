import { type NextRequest, NextResponse } from "next/server";

// KPI summary route — proxies to Omni Gateway /kpi/summary.
// NO mock fallback: returns an honest 502 error when the gateway is unreachable.

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

function gatewayError(detail: string) {
  return NextResponse.json({ source: "error", error: detail }, { status: 502 });
}

export async function GET(request: NextRequest) {
  const tenantId = request.nextUrl.searchParams.get("tenant_id");
  if (!GATEWAY_URL) {
    return gatewayError("OMNI_GATEWAY_URL not configured");
  }
  const authHeader: HeadersInit = GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {};
  const tenantParam = tenantId ? `&tenant_id=${encodeURIComponent(tenantId)}` : "";
  try {
    const [summaryRes, trendRes] = await Promise.all([
      fetch(`${GATEWAY_URL}/kpi/summary?${tenantParam}`, { headers: authHeader, next: { revalidate: 30 } }),
      fetch(`${GATEWAY_URL}/kpi/trend?window=24h${tenantParam}`, { headers: authHeader, next: { revalidate: 30 } }),
    ]);
    if (!summaryRes.ok) return gatewayError(`gateway /kpi/summary ${summaryRes.status}`);
    const summary = await summaryRes.json();
    const trendData = trendRes.ok ? await trendRes.json() : { lanes: {} };
    const trend = Object.entries(trendData.lanes ?? {}).map(([lane, v]: [string, unknown]) => ({
      lane,
      detected: (v as { detected: number }).detected ?? 0,
      resolved: (v as { resolved: number }).resolved ?? 0,
    }));
    return NextResponse.json({ ...summary, source: "gateway", trend });
  } catch {
    return gatewayError("gateway /kpi unreachable");
  }
}
