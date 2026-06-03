import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export interface EpsResponse {
  generated_at: string;
  window_seconds: number;
  total_eps: number;
  agents: Record<string, number>;
  source: "gateway" | "error";
}

function gatewayError(detail: string) {
  return NextResponse.json({ source: "error", error: detail, agents: {}, total_eps: 0 }, { status: 502 });
}

export async function GET() {
  if (!GATEWAY_URL) {
    return gatewayError("OMNI_GATEWAY_URL not configured");
  }
  try {
    const res = await fetch(`${GATEWAY_URL}/agents/remote/eps`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
    });
    if (!res.ok) return gatewayError(`gateway /agents/remote/eps ${res.status}`);
    const data = await res.json();
    return NextResponse.json({ ...data, source: "gateway" } as EpsResponse);
  } catch {
    return gatewayError("gateway /agents/remote/eps unreachable");
  }
}
