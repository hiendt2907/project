import { NextResponse } from "next/server";

// CRAT audit chain proxy → GET /crat/export?format=json&days=1
// Falls back to empty list when gateway unavailable.

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export const dynamic = "force-dynamic";

export interface CratBlock {
  seq: number;
  timestamp: string;
  event_type: string;
  trace_id: string;
  tenant_id: string;
  block_hash: string;
  prev_hash: string;
  has_signature: boolean;
}

export interface CratResponse {
  blocks: CratBlock[];
  total: number;
  source: "gateway" | "error";
}

function gatewayError(detail: string) {
  return NextResponse.json({ source: "error", error: detail, blocks: [], total: 0 }, { status: 502 });
}

export async function GET() {
  if (!GATEWAY_URL) {
    return gatewayError("OMNI_GATEWAY_URL not configured");
  }
  try {
    const res = await fetch(`${GATEWAY_URL}/crat/export?format=json&days=1`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
    });
    if (!res.ok) return gatewayError(`gateway /crat/export ${res.status}`);
    const data = await res.json();
    const blocks: CratBlock[] = ((data.blocks ?? []) as Record<string, string>[])
      .map((b) => ({
        seq: parseInt(b.seq, 10) || 0,
        timestamp: b.timestamp ?? "",
        event_type: b.event_type ?? "",
        trace_id: b.trace_id ?? "",
        tenant_id: b.tenant_id ?? "default",
        block_hash: b.block_hash ?? "",
        prev_hash: b.prev_hash ?? "",
        has_signature: b.has_signature === "true",
      }))
      .sort((a, b) => b.seq - a.seq)
      .slice(0, 10);
    return NextResponse.json({ blocks, total: data.total ?? blocks.length, source: "gateway" } satisfies CratResponse);
  } catch {
    return gatewayError("gateway /crat/export unreachable");
  }
}
