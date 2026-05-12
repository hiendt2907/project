import { NextResponse } from "next/server";

// Proxies to Omni Gateway /agents (real worker heartbeat data).
// Set OMNI_GATEWAY_URL=http://omni-gateway:8000 in production.

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

function authHeaders(): HeadersInit {
  return GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {};
}

export const dynamic = "force-dynamic";

export async function GET() {
  if (!GATEWAY_URL) {
    return NextResponse.json(
      { error: "OMNI_GATEWAY_URL not configured", agents: [], overall: "unknown" },
      { status: 503 }
    );
  }
  try {
    const res = await fetch(`${GATEWAY_URL}/agents`, {
      headers: authHeaders(),
      cache: "no-store",
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      return NextResponse.json(
        { error: `Gateway /agents ${res.status}`, agents: [], overall: "unknown", detail },
        { status: res.status }
      );
    }
    return NextResponse.json(await res.json());
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: `Gateway unreachable: ${msg}`, agents: [], overall: "unknown" },
      { status: 502 }
    );
  }
}
