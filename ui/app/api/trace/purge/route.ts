import { NextResponse } from "next/server";

// Self-service purge for the pipeline dashboard — clears omni:trace:* state so an
// operator can wipe noisy/stuck Active Traces without asking engineering to do it
// by hand. Proxies POST {gateway}/trace/purge.

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export const dynamic = "force-dynamic";

export interface PurgeResponse {
  purged: boolean;
  keys_deleted: number;
  source: "gateway" | "error";
}

export async function POST() {
  if (!GATEWAY_URL) {
    return NextResponse.json(
      { purged: false, keys_deleted: 0, source: "error" } satisfies PurgeResponse,
      { status: 503 },
    );
  }

  try {
    const res = await fetch(`${GATEWAY_URL}/trace/purge`, {
      method: "POST",
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
    });

    if (!res.ok) {
      return NextResponse.json(
        { purged: false, keys_deleted: 0, source: "error" } satisfies PurgeResponse,
        { status: res.status },
      );
    }

    const data = await res.json();
    return NextResponse.json({
      purged: Boolean(data.purged),
      keys_deleted: Number(data.keys_deleted ?? 0),
      source: "gateway",
    } satisfies PurgeResponse);
  } catch {
    return NextResponse.json(
      { purged: false, keys_deleted: 0, source: "error" } satisfies PurgeResponse,
      { status: 502 },
    );
  }
}
