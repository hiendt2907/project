import { type NextRequest } from "next/server";
import { gatewayError, proxyBody, proxyGet } from "@/lib/gateway-proxy";

// Runtime Flags proxy → gateway /autonomy/flags.
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const tenantId = request.nextUrl.searchParams.get("tenant_id") ?? "default";
  return proxyGet(`/autonomy/flags?tenant_id=${encodeURIComponent(tenantId)}`);
}

export async function POST(request: NextRequest) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return gatewayError("invalid JSON body", 400);
  }
  return proxyBody("/autonomy/flags", "POST", body);
}
