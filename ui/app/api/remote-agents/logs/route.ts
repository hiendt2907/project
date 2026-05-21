import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export async function GET(request: NextRequest) {
  const agentId = request.nextUrl.searchParams.get("agent_id");
  const nRaw = request.nextUrl.searchParams.get("n") ?? "50";
  const n = Math.min(500, Math.max(1, parseInt(nRaw, 10) || 50));

  if (!agentId) {
    return NextResponse.json({ error: "agent_id required" }, { status: 400 });
  }

  if (!GATEWAY_URL) {
    return NextResponse.json({
      agent_id: agentId,
      generated_at: new Date().toISOString(),
      logs: [],
      metrics: null,
      source: "mock",
    });
  }

  try {
    const res = await fetch(
      `${GATEWAY_URL}/agents/remote/${encodeURIComponent(agentId)}/logs?n=${String(n)}`,
      {
        headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
        cache: "no-store",
      }
    );
    if (!res.ok) throw new Error(`${res.status}`);
    const data = await res.json();
    return NextResponse.json({ ...data, source: "gateway" });
  } catch {
    return NextResponse.json({
      agent_id: agentId,
      generated_at: new Date().toISOString(),
      logs: [],
      metrics: null,
      source: "error",
    }, { status: 502 });
  }
}
