import { NextResponse } from "next/server";

// Smart SIEM operational telemetry — REAL numbers via Prometheus PromQL.
//
// SIEM_METRICS_URL points at Prometheus `/api/v1/query` (instant query endpoint).
// We run a small set of PromQL queries against the metrics the workers actually
// export (see src/workers/metrics_exporter.py) and map them into the telemetry
// shape the UI consumes. Metrics that have NO exporter source are returned as
// `null` so the UI renders "—" instead of a fabricated number.
//
// NO mock fallback: if Prometheus itself is unreachable we return an honest 502.
//
// Real metrics used:
//   omni_llm_requests_total                   → llm.total_calls_24h, rag denominator
//   omni_llm_client_completion_seconds_bucket → llm.latency_p50_ms / p95_ms (histogram)
//   omni_llm_completion_tokens_total          → llm.tokens_out_total
//   omni_fastpath_hits_total                  → rag fast-path (SOP) hits
//   omni_rag_empty_result_total               → rag empty-result counter
//   omni_kafka_consumer_lag{topic,group}      → pipeline.kafka_lag
// No exporter source (→ null): llm.success_rate, llm.tokens_in_total,
//   rag.avg_query_latency_ms, redis_ops/memory.

export const dynamic = "force-dynamic";

const METRICS_URL = process.env.SIEM_METRICS_URL; // e.g. http://prometheus:9090/api/v1/query

interface PromVector {
  metric: Record<string, string>;
  value: [number, string];
}

let promReachable = false;

async function promQuery(expr: string): Promise<PromVector[] | null> {
  if (!METRICS_URL) return null;
  try {
    const url = `${METRICS_URL}?query=${encodeURIComponent(expr)}`;
    const res = await fetch(url, { next: { revalidate: 5 } });
    if (!res.ok) return null;
    promReachable = true; // an HTTP 200 from Prometheus proves reachability
    const body = (await res.json()) as { status: string; data?: { result?: PromVector[] } };
    if (body.status !== "success") return null;
    return body.data?.result ?? [];
  } catch {
    return null;
  }
}

// Scalar from a single-series instant query; null when the metric is absent.
async function promScalar(expr: string): Promise<number | null> {
  const result = await promQuery(expr);
  if (!result || result.length === 0) return null;
  const v = Number(result[0].value[1]);
  return Number.isFinite(v) ? v : null;
}

function ratio(num: number | null, denom: number | null): number | null {
  if (num === null || denom === null || denom <= 0) return null;
  return num / denom;
}

export async function GET() {
  if (!METRICS_URL) {
    return NextResponse.json(
      { source: "error", error: "SIEM_METRICS_URL not configured" },
      { status: 502 }
    );
  }

  const [calls24h, p50s, p95s, tokensOut, tokensIn, fastpath24h, lagVec] = await Promise.all([
    promScalar("sum(increase(omni_llm_requests_total[24h]))"),
    promScalar("histogram_quantile(0.5, sum by (le) (rate(omni_llm_client_completion_seconds_bucket[1h])))"),
    promScalar("histogram_quantile(0.95, sum by (le) (rate(omni_llm_client_completion_seconds_bucket[1h])))"),
    promScalar("sum(increase(omni_llm_completion_tokens_total[24h]))"),
    promScalar("sum(increase(omni_llm_prompt_tokens_total[24h]))"),
    promScalar("sum(increase(omni_fastpath_hits_total[24h]))"),
    promQuery("omni_kafka_consumer_lag"),
  ]);

  // If not a single query reached Prometheus, the source is down — be honest.
  if (!promReachable) {
    return NextResponse.json(
      { source: "error", error: "Prometheus unreachable (SIEM_METRICS_URL)" },
      { status: 502 }
    );
  }

  const ragQueries = calls24h !== null || fastpath24h !== null ? (calls24h ?? 0) + (fastpath24h ?? 0) : null;

  const kafkaLag = (lagVec ?? []).map((s) => ({
    topic: s.metric.topic ?? "",
    group: s.metric.group ?? s.metric.consumer_group ?? "",
    lag: Number(s.value[1]) || 0,
  }));

  return NextResponse.json({
    generated_at: new Date().toISOString(),
    source: "prometheus",
    llm: {
      total_calls_24h: calls24h !== null ? Math.round(calls24h) : null,
      success_rate: null, // no outcome-labelled metric exported yet
      latency_p50_ms: p50s !== null ? Math.round(p50s * 1000) : null,
      latency_p95_ms: p95s !== null ? Math.round(p95s * 1000) : null,
      tokens_in_total: tokensIn !== null ? Math.round(tokensIn) : null,
      tokens_out_total: tokensOut !== null ? Math.round(tokensOut) : null,
    },
    rag: {
      queries_24h: ragQueries !== null ? Math.round(ragQueries) : null,
      cache_hit_ratio: ratio(fastpath24h, ragQueries),
      avg_query_latency_ms: null, // no RAG-specific latency histogram
    },
    pipeline: {
      kafka_lag: kafkaLag,
      redis_ops_per_sec: null,
      redis_memory_used_bytes: null,
      redis_memory_max_bytes: null,
    },
  });
}
