// Direct read-only call to the Omni gateway (/siem/overview), separate from the
// provider console backend (lib/config.ts backendConfig) — same split as
// lib/understanding.ts (console BFF) vs. gateway-native data. Server-side only
// (env vars, no cookie forwarding: service-to-service call authenticated by API key).
const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export interface GatewaySectionResult<T> {
  data: T | null;
  error: string | null;
}

// Backed by src/gateway/routes/siem.py `/siem/overview` — read-only CRAT audit-chain
// projection, tenant-scoped via resolve_scope(tenant_id). No mutation endpoint here;
// HITL decisions / kill-chain correlation live in other slices, out of scope for this
// read-only incidents projection.
export interface SiemChainSummary {
  total_blocks: number;
  head_hash_prefix: string | null;
  integrity: "verified" | "empty";
}

export interface SiemRecentBlock {
  seq: number | null;
  event_type: string;
  trace_id: string | null;
  timestamp_utc: string;
  verdict: string;
  root_cause: string;
  affected_workload: string;
  block_hash: string;
}

export interface SiemOverviewResponse {
  generated_at: string;
  chain: SiemChainSummary;
  verdict_distribution_24h: Record<string, number>;
  event_type_distribution: Record<string, number>;
  recent_blocks: SiemRecentBlock[];
}

export async function fetchSiemOverview(
  tenantId: string,
  limit = 20,
): Promise<GatewaySectionResult<SiemOverviewResponse>> {
  if (!GATEWAY_URL) {
    return { data: null, error: "OMNI_GATEWAY_URL not configured" };
  }
  const path = `/siem/overview?tenant_id=${encodeURIComponent(tenantId)}&limit=${limit}`;
  try {
    const res = await fetch(`${GATEWAY_URL}${path}`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) {
      return { data: null, error: `gateway /siem/overview ${res.status}` };
    }
    return { data: (await res.json()) as SiemOverviewResponse, error: null };
  } catch {
    return { data: null, error: "gateway /siem/overview unreachable" };
  }
}
