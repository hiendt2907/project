import { NextRequest, NextResponse } from "next/server";
import { resolveSession } from "@aoip/auth-client";
import { backendConfig } from "@/lib/config";

// Delete a single KB entry — proxies to gateway DELETE /kb/{collection}/{id}
// (src/gateway/routes/kb.py). Only entries in the write collection are actually
// deletable server-side; the gateway itself returns 404 for anything else.

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

async function authorized(request: NextRequest): Promise<boolean> {
  const session = await resolveSession(backendConfig, request.headers.get("cookie") ?? "");
  return session.status === "authenticated";
}

function errorResponse(status: number, detail: string) {
  return NextResponse.json({ error: detail }, { status });
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ collection: string; id: string }> },
) {
  if (!(await authorized(request))) return errorResponse(401, "Authentication required");
  if (!GATEWAY_URL) return errorResponse(502, "OMNI_GATEWAY_URL not configured");
  const { collection, id } = await params;
  try {
    const response = await fetch(
      `${GATEWAY_URL}/kb/${encodeURIComponent(collection)}/${encodeURIComponent(id)}`,
      {
        method: "DELETE",
        headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
        cache: "no-store",
        signal: AbortSignal.timeout(5000),
      },
    );
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return errorResponse(502, "Gateway unreachable");
  }
}
