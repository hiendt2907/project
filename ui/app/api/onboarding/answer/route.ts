import { type NextRequest, NextResponse } from "next/server";

// Answer-question proxy — first WRITE action of the portal.
// Proxies POST /onboarding/questions/{id}/answer to the gateway.
// NO mock fallback: honest error when gateway unreachable or rejects.

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

const QUESTION_ID_PATTERN = /^[A-Za-z0-9:_.-]{1,128}$/;

interface AnswerRequestBody {
  question_id?: string;
  answered_by?: string;
  value?: string;
  tenant_id?: string;
}

export async function POST(request: NextRequest) {
  if (!GATEWAY_URL) {
    return NextResponse.json({ error: "OMNI_GATEWAY_URL not configured" }, { status: 502 });
  }
  let body: AnswerRequestBody;
  try {
    body = (await request.json()) as AnswerRequestBody;
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const questionId = body.question_id ?? "";
  const answeredBy = (body.answered_by ?? "").trim();
  const value = (body.value ?? "").trim();
  if (!QUESTION_ID_PATTERN.test(questionId)) {
    return NextResponse.json({ error: "invalid question_id" }, { status: 400 });
  }
  if (!answeredBy || answeredBy.length > 120) {
    return NextResponse.json({ error: "answered_by is required (max 120 chars)" }, { status: 400 });
  }
  if (!value || value.length > 500) {
    return NextResponse.json({ error: "value is required (max 500 chars)" }, { status: 400 });
  }

  try {
    const res = await fetch(
      `${GATEWAY_URL}/onboarding/questions/${encodeURIComponent(questionId)}/answer`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {}),
        },
        body: JSON.stringify({
          answered_by: answeredBy,
          value,
          source_channel: "portal",
          ...(body.tenant_id ? { tenant_id: body.tenant_id } : {}),
        }),
        cache: "no-store",
        signal: AbortSignal.timeout(5000),
      },
    );
    const payload = (await res.json().catch(() => null)) as { detail?: string } | null;
    if (!res.ok) {
      return NextResponse.json(
        { error: payload?.detail ?? `gateway answer ${res.status}` },
        { status: res.status },
      );
    }
    return NextResponse.json(payload);
  } catch {
    return NextResponse.json({ error: "gateway answer unreachable" }, { status: 502 });
  }
}
