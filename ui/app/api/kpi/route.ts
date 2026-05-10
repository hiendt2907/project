import { NextResponse } from "next/server";

// KPI summary route — proxies to Omni Gateway /kpi/summary or returns mock.
// Set OMNI_GATEWAY_URL=http://omni-gateway:8000 for production.

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

type KpiSummary = {
  generated_at: string;
  window: string;
  source: "gateway" | "mock";
  advisory: {
    accepted: number;
    rejected: number;
    total: number;
    acceptance_rate: number | null;
  };
  execution: {
    total_executed: number;
    false_positive: number;
    false_positive_rate: number | null;
  };
  trend: {
    lane: string;
    detected: number;
    resolved: number;
  }[];
};

function mockKpi(): KpiSummary {
  return {
    generated_at: new Date().toISOString(),
    window: "24h",
    source: "mock",
    advisory: {
      accepted: 142,
      rejected: 23,
      total: 165,
      acceptance_rate: 0.8606,
    },
    execution: {
      total_executed: 142,
      false_positive: 8,
      false_positive_rate: 0.0563,
    },
    trend: [
      { lane: "SYS_RESOURCE", detected: 48, resolved: 45 },
      { lane: "SYS_HARD_FAIL", detected: 31, resolved: 28 },
      { lane: "APP_HTTP", detected: 62, resolved: 59 },
      { lane: "SIEM_SECURITY", detected: 24, resolved: 20 },
    ],
  };
}

export async function GET() {
  if (!GATEWAY_URL) {
    return NextResponse.json(mockKpi());
  }
  try {
    const [summaryRes, trendRes] = await Promise.all([
      fetch(`${GATEWAY_URL}/kpi/summary`, {
        headers: GATEWAY_API_KEY ? { "X-Api-Key": GATEWAY_API_KEY } : {},
        next: { revalidate: 30 },
      }),
      fetch(`${GATEWAY_URL}/kpi/trend?window=24h`, {
        headers: GATEWAY_API_KEY ? { "X-Api-Key": GATEWAY_API_KEY } : {},
        next: { revalidate: 30 },
      }),
    ]);
    if (!summaryRes.ok) throw new Error(`gateway /kpi/summary ${summaryRes.status}`);
    const summary = await summaryRes.json();
    const trendData = trendRes.ok ? await trendRes.json() : { lanes: {} };
    const trend = Object.entries(trendData.lanes ?? {}).map(([lane, v]: [string, unknown]) => ({
      lane,
      detected: (v as { detected: number }).detected ?? 0,
      resolved: (v as { resolved: number }).resolved ?? 0,
    }));
    return NextResponse.json({ ...summary, source: "gateway", trend });
  } catch {
    return NextResponse.json(mockKpi());
  }
}
