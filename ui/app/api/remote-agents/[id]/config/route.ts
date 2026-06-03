import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const PROVISIONER = process.env.OMNI_PROVISIONER_URL ?? "http://host.orb.internal:9901";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  await params;
  const serverIp = request.nextUrl.searchParams.get("server_ip");
  if (!serverIp) return NextResponse.json({ error: "server_ip required" }, { status: 400 });

  try {
    const res = await fetch(`${PROVISIONER}/agent/${encodeURIComponent(serverIp)}/config`);
    return NextResponse.json(await res.json(), { status: res.status });
  } catch (err: unknown) {
    return NextResponse.json({ error: String(err) }, { status: 502 });
  }
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  await params;
  const serverIp = request.nextUrl.searchParams.get("server_ip");
  if (!serverIp) return NextResponse.json({ error: "server_ip required" }, { status: 400 });

  try {
    const body = await request.json();
    const res = await fetch(`${PROVISIONER}/agent/${encodeURIComponent(serverIp)}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return NextResponse.json(await res.json(), { status: res.status });
  } catch (err: unknown) {
    return NextResponse.json({ error: String(err) }, { status: 502 });
  }
}
