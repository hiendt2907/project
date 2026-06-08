import { type NextRequest } from "next/server";
import { proxyGet } from "@/lib/gateway-proxy";

// HITL pending queue proxy → gateway /autonomy/hitl/pending.
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const tenantId = request.nextUrl.searchParams.get("tenant_id") ?? "default";
  return proxyGet(`/autonomy/hitl/pending?tenant_id=${encodeURIComponent(tenantId)}`);
}
