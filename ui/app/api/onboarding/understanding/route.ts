import { type NextRequest, NextResponse } from "next/server";

// Understanding aggregate route — proxies gateway /onboarding read endpoints
// (entities + unknowns + questions + readiness) in parallel.
// NO mock fallback: each section reports an honest error when unreachable.

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

interface SectionResult {
  data: unknown | null;
  error: string | null;
}

async function fetchSection(path: string): Promise<SectionResult> {
  try {
    const res = await fetch(`${GATEWAY_URL}${path}`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) return { data: null, error: `gateway ${path.split("?")[0]} ${res.status}` };
    return { data: await res.json(), error: null };
  } catch {
    return { data: null, error: `gateway ${path.split("?")[0]} unreachable` };
  }
}

export async function GET(request: NextRequest) {
  if (!GATEWAY_URL) {
    return NextResponse.json({ source: "error", error: "OMNI_GATEWAY_URL not configured" }, { status: 502 });
  }
  const tenantId = request.nextUrl.searchParams.get("tenant_id");
  const q = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : "";
  const [entities, unknowns, questions, readiness, diagram, agents] = await Promise.all([
    fetchSection(`/onboarding/entities${q}`),
    fetchSection(`/onboarding/unknowns${q}`),
    fetchSection(`/onboarding/questions${q}`),
    fetchSection(`/onboarding/readiness${q}`),
    fetchSection(`/onboarding/diagram${q}`),
    fetchSection(`/webhook/agent/versions${q}`),
  ]);
  return NextResponse.json({ source: "gateway", entities, unknowns, questions, readiness, diagram, agents });
}
