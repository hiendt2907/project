import { NextResponse } from "next/server";

// Smart SIEM operational telemetry.
// Production deployment: this route reads from the metrics-exporter sidecar
// (Prometheus /metrics HTTP endpoint) via Env var SIEM_METRICS_URL, falls back
// to the mock shape below when the sidecar is unreachable (dev / disconnected).
//
// Backend contract (Prometheus counter/histogram names this shape mirrors):
//   omni_siem_events_ingested_total{severity,category,tenant}
//   omni_siem_chain_detected_total{category}
//   omni_llm_requests_total{model,outcome}
//   omni_llm_request_duration_seconds{model}  (histogram, p50/p95/p99)
//   omni_llm_tokens_total{model,direction}
//   omni_rag_query_total{collection,outcome}
//   omni_semantic_cache_hit_total, omni_semantic_cache_miss_total
//   omni_playbook_match_total{playbook_id,outcome}
//   omni_hitl_pending_gauge
//   omni_kafka_consumer_lag{topic,group}
//   omni_worker_heartbeat_timestamp{role}

const METRICS_URL = process.env.SIEM_METRICS_URL;

type SiemOverview = {
  generated_at: string;
  source: "prometheus" | "mock";
  ingestion: {
    total_last_24h: number;
    rate_per_min: number;
    by_severity: { critical: number; high: number; medium: number; low: number; info: number };
    by_category: { name: string; count: number }[];
    by_tenant: { tenant: string; count: number }[];
    timeline_24h: { hour: string; events: number; incidents: number }[];
  };
  correlation: {
    chains_detected_24h: number;
    active_windows: number;
    chains_by_category: { category: string; count: number }[];
    last_chain_trace_id: string;
    last_chain_detected_at: string;
  };
  llm: {
    total_calls_24h: number;
    success_rate: number;
    failure_count: number;
    latency_p50_ms: number;
    latency_p95_ms: number;
    latency_p99_ms: number;
    tokens_in_total: number;
    tokens_out_total: number;
    by_model: { model: string; calls: number; avg_latency_ms: number; tokens: number; failures: number }[];
    latency_timeline: { hour: string; p50: number; p95: number }[];
    last_call_trace: string;
  };
  rag: {
    queries_24h: number;
    cache_hits: number;
    cache_misses: number;
    cache_hit_ratio: number;
    avg_query_latency_ms: number;
    by_collection: { name: string; queries: number; hit_ratio: number; vectors: number }[];
    top_queries: { query: string; count: number; avg_distance: number }[];
  };
  playbook: {
    matches_24h: number;
    auto_executed: number;
    hitl_gated: number;
    no_match: number;
    by_playbook: { id: string; name: string; matches: number; success: number; failures: number }[];
  };
  hitl: {
    pending: number;
    approved_24h: number;
    rejected_24h: number;
    timed_out_24h: number;
    avg_approval_time_sec: number;
    queue: { incident_id: string; category: string; severity: string; waiting_sec: number; trace_id: string }[];
  };
  pipeline: {
    kafka_lag: { topic: string; group: string; lag: number }[];
    redis_ops_per_sec: number;
    redis_memory_used_bytes: number;
    redis_memory_max_bytes: number;
    workers: { role: string; replicas: number; ready: number; last_heartbeat_age_sec: number }[];
  };
};

function rand(min: number, max: number) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function buildMock(): SiemOverview {
  const now = new Date();
  const hours = Array.from({ length: 24 }, (_, i) => {
    const d = new Date(now.getTime() - (23 - i) * 3600_000);
    return `${String(d.getHours()).padStart(2, "0")}:00`;
  });

  return {
    generated_at: now.toISOString(),
    source: "mock",
    ingestion: {
      total_last_24h: 14_832,
      rate_per_min: 10.3,
      by_severity: { critical: 38, high: 412, medium: 3_104, low: 8_221, info: 3_057 },
      by_category: [
        { name: "network_anomaly", count: 4_201 },
        { name: "auth_failure", count: 3_815 },
        { name: "k8s_threat", count: 2_940 },
        { name: "malware", count: 1_822 },
        { name: "lateral_movement", count: 1_203 },
        { name: "data_exfil", count: 489 },
        { name: "ddos", count: 362 },
      ],
      by_tenant: [
        { tenant: "tenant-acme", count: 6_120 },
        { tenant: "tenant-globex", count: 4_433 },
        { tenant: "tenant-initech", count: 2_481 },
        { tenant: "tenant-umbrella", count: 1_798 },
      ],
      timeline_24h: hours.map((h) => ({ hour: h, events: rand(380, 980), incidents: rand(1, 14) })),
    },
    correlation: {
      chains_detected_24h: 87,
      active_windows: 14,
      chains_by_category: [
        { category: "lateral_movement", count: 28 },
        { category: "data_exfil", count: 19 },
        { category: "k8s_threat", count: 17 },
        { category: "auth_failure", count: 12 },
        { category: "malware", count: 11 },
      ],
      last_chain_trace_id: `fg-e2e-chain-${rand(10000, 99999)}`,
      last_chain_detected_at: new Date(now.getTime() - rand(60, 3600) * 1000).toISOString(),
    },
    llm: {
      total_calls_24h: 2_947,
      success_rate: 98.4,
      failure_count: 47,
      latency_p50_ms: 812,
      latency_p95_ms: 2_410,
      latency_p99_ms: 4_180,
      tokens_in_total: 1_842_500,
      tokens_out_total: 412_900,
      by_model: [
        { model: "ollama/llama3.1:8b", calls: 1_980, avg_latency_ms: 720, tokens: 1_520_000, failures: 18 },
        { model: "ollama/mistral:7b", calls: 612, avg_latency_ms: 880, tokens: 420_000, failures: 9 },
        { model: "vllm/qwen2.5:14b", calls: 355, avg_latency_ms: 1_380, tokens: 315_400, failures: 20 },
      ],
      latency_timeline: hours.map((h) => ({ hour: h, p50: rand(600, 1100), p95: rand(1800, 3200) })),
      last_call_trace: `llm-tr-${rand(100000, 999999)}`,
    },
    rag: {
      queries_24h: 3_812,
      cache_hits: 2_543,
      cache_misses: 1_269,
      cache_hit_ratio: 66.7,
      avg_query_latency_ms: 38,
      by_collection: [
        { name: "k8s_expert", queries: 1_830, hit_ratio: 72.1, vectors: 18_420 },
        { name: "sop_runbooks", queries: 980, hit_ratio: 81.4, vectors: 5_234 },
        { name: "incident_history", queries: 620, hit_ratio: 54.2, vectors: 9_871 },
        { name: "errors", queries: 240, hit_ratio: 44.8, vectors: 1_203 },
        { name: "semcache", queries: 142, hit_ratio: 98.2, vectors: 442 },
      ],
      top_queries: [
        { query: "CrashLoopBackOff remediation for stateless deployments", count: 142, avg_distance: 0.142 },
        { query: "NetworkPolicy egress denial — lateral movement", count: 98, avg_distance: 0.189 },
        { query: "OOMKill memory tuning runbook", count: 87, avg_distance: 0.212 },
        { query: "Privileged container escape detection", count: 64, avg_distance: 0.241 },
        { query: "Kafka consumer lag triage", count: 51, avg_distance: 0.267 },
      ],
    },
    playbook: {
      matches_24h: 412,
      auto_executed: 284,
      hitl_gated: 98,
      no_match: 30,
      by_playbook: [
        { id: "pb-001", name: "K8s Pod CrashLoop Remediation", matches: 142, success: 138, failures: 4 },
        { id: "pb-002", name: "Privileged Container Breakout", matches: 38, success: 35, failures: 3 },
        { id: "pb-003", name: "High CPU Scaling", matches: 96, success: 94, failures: 2 },
        { id: "pb-004", name: "OOM Kill Recovery", matches: 78, success: 76, failures: 2 },
        { id: "pb-005", name: "Network Policy Violation", matches: 58, success: 52, failures: 6 },
      ],
    },
    hitl: {
      pending: 3,
      approved_24h: 68,
      rejected_24h: 11,
      timed_out_24h: 4,
      avg_approval_time_sec: 182,
      queue: [
        { incident_id: "fg-inc-a4b201", category: "k8s_threat", severity: "critical", waiting_sec: 142, trace_id: "fg-a4b201ef" },
        { incident_id: "fg-inc-c81d44", category: "data_exfil", severity: "critical", waiting_sec: 88, trace_id: "fg-c81d44a2" },
        { incident_id: "fg-inc-ee9c03", category: "malware", severity: "critical", waiting_sec: 22, trace_id: "fg-ee9c0371" },
      ],
    },
    pipeline: {
      kafka_lag: [
        { topic: "omni-alerts", group: "omni-prober", lag: 4 },
        { topic: "omni-diagnostic-evidence", group: "omni-analyst", lag: 2 },
        { topic: "omni-actions", group: "omni-executor", lag: 0 },
        { topic: "omni-hitl-pending", group: "omni-hitl-dispatcher", lag: 3 },
      ],
      redis_ops_per_sec: 274,
      redis_memory_used_bytes: 1_374_389_534,
      redis_memory_max_bytes: 2_147_483_648,
      workers: [
        { role: "prober", replicas: 2, ready: 2, last_heartbeat_age_sec: 3 },
        { role: "analyst", replicas: 3, ready: 3, last_heartbeat_age_sec: 2 },
        { role: "core", replicas: 1, ready: 1, last_heartbeat_age_sec: 5 },
        { role: "executor", replicas: 2, ready: 2, last_heartbeat_age_sec: 1 },
        { role: "gateway", replicas: 2, ready: 2, last_heartbeat_age_sec: 2 },
        { role: "siem-bridge", replicas: 1, ready: 1, last_heartbeat_age_sec: 1 },
        { role: "evidence-adapter", replicas: 1, ready: 1, last_heartbeat_age_sec: 4 },
        { role: "hitl-dispatcher", replicas: 1, ready: 1, last_heartbeat_age_sec: 3 },
      ],
    },
  };
}

async function fetchPrometheus(): Promise<SiemOverview | null> {
  if (!METRICS_URL) return null;
  try {
    const res = await fetch(METRICS_URL, { next: { revalidate: 5 } });
    if (!res.ok) return null;
    // Parsing prometheus text format left to production adapter.
    // When SIEM_METRICS_URL points to a JSON-aggregator sidecar (recommended),
    // upstream emits the same shape as buildMock() directly.
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) {
      const data = (await res.json()) as SiemOverview;
      return { ...data, source: "prometheus" };
    }
    return null;
  } catch {
    return null;
  }
}

export async function GET() {
  const real = await fetchPrometheus();
  return NextResponse.json(real ?? buildMock());
}
