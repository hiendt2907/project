import { NextRequest, NextResponse } from "next/server";
import { resolveSession } from "@aoip/auth-client";
import { backendConfig } from "@/lib/config";

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

export async function GET(request: NextRequest) {
  if (!(await authorized(request))) return errorResponse(401, "Authentication required");
  if (!GATEWAY_URL) return errorResponse(502, "OMNI_GATEWAY_URL not configured");
  const tenantId = request.nextUrl.searchParams.get("tenant_id") ?? "default";
  try {
    const response = await fetch(`${GATEWAY_URL}/autonomy/mutation?tenant_id=${encodeURIComponent(tenantId)}`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return errorResponse(502, "Gateway unreachable");
  }
}

export async function POST(request: NextRequest) {
  if (!(await authorized(request))) return errorResponse(401, "Authentication required");
  if (!GATEWAY_URL) return errorResponse(502, "OMNI_GATEWAY_URL not configured");
  const body = await request.json().catch(() => null);
  if (!body || typeof body.tenant_id !== "string" || typeof body.enabled !== "boolean") {
    return errorResponse(400, "tenant_id and enabled are required");
  }
  try {
    const response = await fetch(`${GATEWAY_URL}/autonomy/mutation`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {}),
      },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return errorResponse(502, "Gateway unreachable");
  }
}
