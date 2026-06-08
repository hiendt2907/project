import { type NextRequest } from "next/server";
import { gatewayError, proxyBody } from "@/lib/gateway-proxy";

// HITL decide proxy → gateway POST /autonomy/hitl/{id}/decide.
export const dynamic = "force-dynamic";

export async function POST(request: NextRequest, { params }: { params: Promise<{ pendingId: string }> }) {
  const { pendingId } = await params;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return gatewayError("invalid JSON body", 400);
  }
  return proxyBody(`/autonomy/hitl/${encodeURIComponent(pendingId)}/decide`, "POST", body);
}
