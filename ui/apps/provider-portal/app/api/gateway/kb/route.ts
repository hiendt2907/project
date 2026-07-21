import { NextRequest, NextResponse } from "next/server";
import { resolveSession } from "@aoip/auth-client";
import { backendConfig } from "@/lib/config";

// RAG Knowledge-Base proxy — list/create vendor knowledge directly against the Omni
// gateway (src/gateway/routes/kb.py). Cluster-global by design (KbCreate has no
// tenant_id): unlike /autonomy/mutation this never carries a tenant_id, mirroring the
// backend contract exactly (see docs audit for admin/kb port). Session-gated like
// /api/gateway/autonomy/mutation — any authenticated provider session may read/write;
// the gateway itself has no per-permission RBAC on this route.

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
  // Pass the caller's limit through as-is; no default is imposed here so the
  // gateway's own clamp (10-500, default 200) stays the single source of truth.
  const limit = request.nextUrl.searchParams.get("limit");
  const search = limit ? `?limit=${encodeURIComponent(limit)}` : "";
  try {
    const response = await fetch(`${GATEWAY_URL}/kb${search}`, {
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
  if (!body || typeof body.title !== "string" || typeof body.knowledge !== "string") {
    return errorResponse(400, "title and knowledge are required");
  }
  try {
    const response = await fetch(`${GATEWAY_URL}/kb`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {}),
      },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return errorResponse(502, "Gateway unreachable");
  }
}
