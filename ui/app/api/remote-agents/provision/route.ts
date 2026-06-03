import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

// OrbStack exposes Mac host at host.orb.internal from inside pods
const PROVISIONER = process.env.OMNI_PROVISIONER_URL ?? "http://host.orb.internal:9901";

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const res = await fetch(`${PROVISIONER}/provision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `Provisioner unreachable: ${msg}` }, { status: 502 });
  }
}

export async function GET(request: NextRequest) {
  const taskId = request.nextUrl.searchParams.get("task_id");
  if (!taskId) return NextResponse.json({ error: "task_id required" }, { status: 400 });

  // Proxy SSE stream from provisioner
  try {
    const upstream = await fetch(`${PROVISIONER}/provision/${taskId}/stream`, {
      headers: { Accept: "text/event-stream" },
    });
    if (!upstream.ok) {
      return NextResponse.json({ error: "Task not found" }, { status: 404 });
    }
    return new Response(upstream.body, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      },
    });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `Provisioner unreachable: ${msg}` }, { status: 502 });
  }
}
