import { type NextRequest } from "next/server";
import { proxyBody } from "@/lib/gateway-proxy";

// Tenant API-key revoke proxy → gateway DELETE /autonomy/tenants/{id}/api-keys/{keyId}.
export const dynamic = "force-dynamic";

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ tenantId: string; keyId: string }> },
) {
  const { tenantId, keyId } = await params;
  return proxyBody(
    `/autonomy/tenants/${encodeURIComponent(tenantId)}/api-keys/${encodeURIComponent(keyId)}`,
    "DELETE",
  );
}
