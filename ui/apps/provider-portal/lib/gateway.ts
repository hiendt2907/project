// Direct read-only calls to the Omni gateway (/onboarding/*), separate from the
// provider console backend (lib/config.ts backendConfig). Understanding-page
// readiness + system-diagram data lives on the gateway, not the console API —
// see src/gateway/routes/onboarding.py. Server-side only (env vars, no cookie
// forwarding needed: service-to-service call authenticated by API key).

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export interface GatewaySectionResult<T> {
  data: T | null;
  error: string | null;
}

export async function fetchGatewaySection<T>(path: string): Promise<GatewaySectionResult<T>> {
  if (!GATEWAY_URL) {
    return { data: null, error: "OMNI_GATEWAY_URL not configured" };
  }
  try {
    const res = await fetch(`${GATEWAY_URL}${path}`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) {
      return { data: null, error: `gateway ${path.split("?")[0]} ${res.status}` };
    }
    return { data: (await res.json()) as T, error: null };
  } catch {
    return { data: null, error: `gateway ${path.split("?")[0]} unreachable` };
  }
}
