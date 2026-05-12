import { NextRequest, NextResponse } from "next/server";

// HITL approve/reject proxy → Omni Gateway /playbooks/{id}/approve|reject
const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export const dynamic = "force-dynamic";

function authHeaders(): HeadersInit {
  return {
    "Content-Type": "application/json",
    ...(GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {}),
  };
}

export async function POST(req: NextRequest) {
  const { incident_id, decision, trace_id, reason } = await req.json().catch(() => ({}));
  if (!incident_id || !decision || !["approved", "rejected"].includes(decision)) {
    return NextResponse.json(
      { error: "incident_id and decision (approved|rejected) are required" },
      { status: 400 }
    );
  }
  if (!GATEWAY_URL) {
    return NextResponse.json({ error: "OMNI_GATEWAY_URL not configured" }, { status: 503 });
  }
  const endpoint = decision === "approved" ? "approve" : "reject";
  try {
    const res = await fetch(`${GATEWAY_URL}/playbooks/${incident_id}/${endpoint}`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ trace_id: trace_id ?? "", reason: reason ?? "" }),
    });
    const body = await res.json().catch(() => ({}));
    return NextResponse.json(body, { status: res.status });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `Gateway unreachable: ${msg}` }, { status: 502 });
  }
}
