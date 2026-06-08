import { type NextRequest } from "next/server";
import { gatewayError, proxyBody, proxyGet } from "@/lib/gateway-proxy";

// Risk-Class Matrix proxy → gateway /autonomy/risk-class.
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const tenantId = request.nextUrl.searchParams.get("tenant_id") ?? "default";
  return proxyGet(`/autonomy/risk-class?tenant_id=${encodeURIComponent(tenantId)}`);
}

export async function POST(request: NextRequest) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return gatewayError("invalid JSON body", 400);
  }
  return proxyBody("/autonomy/risk-class", "POST", body);
}
