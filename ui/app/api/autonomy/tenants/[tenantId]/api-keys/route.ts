import { type NextRequest } from "next/server";
import { proxyBody, proxyGet } from "@/lib/gateway-proxy";

// Tenant API-key list/create proxy → gateway /autonomy/tenants/{id}/api-keys.
export const dynamic = "force-dynamic";

export async function GET(_request: NextRequest, { params }: { params: Promise<{ tenantId: string }> }) {
  const { tenantId } = await params;
  return proxyGet(`/autonomy/tenants/${encodeURIComponent(tenantId)}/api-keys`);
}

export async function POST(request: NextRequest, { params }: { params: Promise<{ tenantId: string }> }) {
  const { tenantId } = await params;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    body = {};
  }
  return proxyBody(`/autonomy/tenants/${encodeURIComponent(tenantId)}/api-keys`, "POST", body);
}
