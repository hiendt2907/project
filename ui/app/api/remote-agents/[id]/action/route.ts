import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const PROVISIONER = process.env.OMNI_PROVISIONER_URL ?? "http://host.orb.internal:9901";
const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

// POST /api/remote-agents/[id]/action?action=restart&server_ip=10.x.x.x
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id: agentId } = await params;
  const action = request.nextUrl.searchParams.get("action");
  const serverIp = request.nextUrl.searchParams.get("server_ip");

  // Deregister goes to gateway (no SSH needed)
  if (action === "deregister") {
    if (!GATEWAY_URL) return NextResponse.json({ error: "Gateway not configured" }, { status: 503 });
    try {
      const res = await fetch(`${GATEWAY_URL}/agents/remote/${encodeURIComponent(agentId)}`, {
        method: "DELETE",
        headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      });
      return NextResponse.json(await res.json(), { status: res.status });
    } catch (err: unknown) {
      return NextResponse.json({ error: String(err) }, { status: 502 });
    }
  }

  // Update goes to gateway command channel (no SSH needed)
  if (action === "update") {
    if (!GATEWAY_URL) return NextResponse.json({ error: "Gateway not configured" }, { status: 503 });
    try {
      const body = await request.json() as { version?: string; download_url?: string; sha256_checksum?: string };
      const res = await fetch(`${GATEWAY_URL}/webhook/agent/update`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {}),
        },
        body: JSON.stringify({
          agent_id: agentId,
          version: body.version ?? "",
          download_url: body.download_url ?? "",
          sha256_checksum: body.sha256_checksum ?? "",
        }),
      });
      return NextResponse.json(await res.json(), { status: res.status });
    } catch (err: unknown) {
      return NextResponse.json({ error: String(err) }, { status: 502 });
    }
  }

  // SSH-based actions go to provisioner
  if (!serverIp) return NextResponse.json({ error: "server_ip required" }, { status: 400 });
  const validActions = ["restart", "stop", "enable", "disable", "uninstall"];
  if (!action || !validActions.includes(action)) {
    return NextResponse.json({ error: `Invalid action. Valid: ${validActions.join(",")}` }, { status: 400 });
  }

  try {
    const res = await fetch(`${PROVISIONER}/agent/${encodeURIComponent(serverIp)}/${action}`, {
      method: "POST",
    });
    return NextResponse.json(await res.json(), { status: res.status });
  } catch (err: unknown) {
    return NextResponse.json({ error: `Provisioner unreachable: ${String(err)}` }, { status: 502 });
  }
}
