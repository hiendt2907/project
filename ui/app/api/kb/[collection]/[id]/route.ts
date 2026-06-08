import { type NextRequest } from "next/server";
import { proxyBody } from "@/lib/gateway-proxy";

// Delete a single KB document → gateway DELETE /kb/{collection}/{id}.
export const dynamic = "force-dynamic";

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ collection: string; id: string }> }
) {
  const { collection, id } = await params;
  return proxyBody(`/kb/${encodeURIComponent(collection)}/${encodeURIComponent(id)}`, "DELETE");
}
