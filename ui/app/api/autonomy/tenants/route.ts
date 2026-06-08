import { type NextRequest } from "next/server";
import { gatewayError, proxyBody, proxyGet } from "@/lib/gateway-proxy";

// Tenant list/create proxy → gateway /autonomy/tenants.
export const dynamic = "force-dynamic";

export async function GET() {
  return proxyGet("/autonomy/tenants");
}

export async function POST(request: NextRequest) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return gatewayError("invalid JSON body", 400);
  }
  return proxyBody("/autonomy/tenants", "POST", body);
}
