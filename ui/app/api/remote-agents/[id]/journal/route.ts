import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const PROVISIONER = process.env.OMNI_PROVISIONER_URL ?? "http://host.orb.internal:9901";

// GET /api/remote-agents/[id]/journal?server_ip=10.x.x.x
// Returns SSE stream proxied from provisioner journalctl stream
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  await params; // agentId unused — server_ip used for SSH
  const serverIp = request.nextUrl.searchParams.get("server_ip");
  if (!serverIp) {
    return new Response("server_ip required", { status: 400 });
  }

  try {
    const upstream = await fetch(
      `${PROVISIONER}/agent/${encodeURIComponent(serverIp)}/journal`,
      { headers: { Accept: "text/event-stream" } }
    );
    return new Response(upstream.body, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      },
    });
  } catch {
    return new Response(`data: ${JSON.stringify({ line: "Provisioner unreachable" })}\n\n`, {
      headers: { "Content-Type": "text/event-stream" },
    });
  }
}
