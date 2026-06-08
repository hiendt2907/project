import { type NextRequest } from "next/server";
import { gatewayError, proxyBody } from "@/lib/gateway-proxy";

// Tenant status proxy → gateway POST /autonomy/tenants/{id}/status.
export const dynamic = "force-dynamic";

export async function POST(request: NextRequest, { params }: { params: Promise<{ tenantId: string }> }) {
  const { tenantId } = await params;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return gatewayError("invalid JSON body", 400);
  }
  return proxyBody(`/autonomy/tenants/${encodeURIComponent(tenantId)}/status`, "POST", body);
}
