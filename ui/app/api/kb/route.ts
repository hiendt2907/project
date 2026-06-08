import { type NextRequest } from "next/server";
import { gatewayError, proxyBody, proxyGet } from "@/lib/gateway-proxy";

// RAG Knowledge-Base proxy → gateway /kb. Lists live from Redis (old + new), creates new entries.
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const limit = request.nextUrl.searchParams.get("limit") ?? "200";
  return proxyGet(`/kb?limit=${encodeURIComponent(limit)}`);
}

export async function POST(request: NextRequest) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return gatewayError("invalid JSON body", 400);
  }
  return proxyBody("/kb", "POST", body);
}
