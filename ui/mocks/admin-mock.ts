// Admin Dashboard — mock data for all 6 sections

export const MOCK_POD_HEALTH = [
  { name: "omni-analyst", status: "healthy", ready: "1/1", age: "7m", restarts: 0, cpu: "420m", mem: "312Mi" },
  { name: "omni-prober", status: "healthy", ready: "1/1", age: "7m", restarts: 0, cpu: "85m", mem: "128Mi" },
  { name: "omni-executor", status: "healthy", ready: "1/1", age: "7m", restarts: 0, cpu: "32m", mem: "96Mi" },
  { name: "omni-core", status: "healthy", ready: "1/1", age: "7m", restarts: 0, cpu: "210m", mem: "256Mi" },
  { name: "omni-gateway", status: "healthy", ready: "1/1", age: "6m", restarts: 0, cpu: "18m", mem: "64Mi" },
  { name: "omni-siem-bridge", status: "healthy", ready: "1/1", age: "3h", restarts: 0, cpu: "12m", mem: "48Mi" },
  { name: "omni-evidence-adapter", status: "healthy", ready: "1/1", age: "3h", restarts: 0, cpu: "8m", mem: "48Mi" },
  { name: "omni-hitl-dispatcher", status: "healthy", ready: "1/1", age: "3h", restarts: 0, cpu: "6m", mem: "32Mi" },
  { name: "kafka", status: "healthy", ready: "1/1", age: "5d", restarts: 5, cpu: "180m", mem: "640Mi" },
  { name: "redis", status: "healthy", ready: "1/1", age: "29d", restarts: 31, cpu: "45m", mem: "280Mi" },
  { name: "omni-ui", status: "degraded", ready: "1/1", age: "47h", restarts: 2, cpu: "28m", mem: "192Mi" },
  { name: "nginx-test", status: "healthy", ready: "1/1", age: "5m", restarts: 0, cpu: "153m", mem: "14Mi" },
];

export const MOCK_KPI = {
  acceptance_rate: 0.863,
  false_positive_rate: 0.048,
  total_24h: 187,
  accepted: 161,
  rejected: 26,
  mttd_by_lane: {
    SYS_RESOURCE: 4.2,
    SYS_HARD_FAIL: 2.8,
    APP_HTTP: 1.9,
    SIEM_SECURITY: 3.5,
  },
  mttr_by_lane: {
    SYS_RESOURCE: 18.4,
    SYS_HARD_FAIL: 12.1,
    APP_HTTP: 8.7,
    SIEM_SECURITY: 25.3,
  },
  trend_6h: [
    { hour: "08:00", accepted: 12, rejected: 2, fp: 1 },
    { hour: "09:00", accepted: 18, rejected: 3, fp: 0 },
    { hour: "10:00", accepted: 22, rejected: 4, fp: 2 },
    { hour: "11:00", accepted: 15, rejected: 1, fp: 0 },
    { hour: "12:00", accepted: 28, rejected: 5, fp: 1 },
    { hour: "13:00", accepted: 31, rejected: 4, fp: 2 },
  ],
};

export const MOCK_CRAT_BLOCKS = [
  {
    seq: 4821,
    event_type: "ADVISORY_DECISION",
    trace_id: "gw-prom-6d368e6b3025",
    verdict: "SUGGEST_REMEDIATION",
    lane: "SYS_HARD_FAIL",
    timestamp: new Date(Date.now() - 3 * 60 * 1000).toISOString(),
    hash: "sha256:9f3d2a8b",
    prev_hash: "sha256:7c2e1b9a",
    signed: false,
  },
  {
    seq: 4820,
    event_type: "ADVISORY_DECISION",
    trace_id: "e2e-siem-03234668",
    verdict: "SUGGEST_REMEDIATION",
    lane: "SIEM_SECURITY",
    timestamp: new Date(Date.now() - 47 * 60 * 1000).toISOString(),
    hash: "sha256:7c2e1b9a",
    prev_hash: "sha256:5a1d0c8b",
    signed: false,
  },
  {
    seq: 4819,
    event_type: "HITL_DECISION",
    trace_id: "gw-prom-5f8d67832713",
    verdict: "APPROVED",
    lane: "SYS_HARD_FAIL",
    timestamp: new Date(Date.now() - 2.1 * 60 * 60 * 1000).toISOString(),
    hash: "sha256:5a1d0c8b",
    prev_hash: "sha256:3f9b7e2c",
    signed: false,
  },
  {
    seq: 4818,
    event_type: "MUTATION_TRAPPED",
    trace_id: "gw-prom-4a3c2b1d0e5f",
    verdict: "BLOCKED",
    lane: "SYS_RESOURCE",
    timestamp: new Date(Date.now() - 3.5 * 60 * 60 * 1000).toISOString(),
    hash: "sha256:3f9b7e2c",
    prev_hash: "sha256:1b5d4c3a",
    signed: false,
  },
  {
    seq: 4817,
    event_type: "ADVISORY_DECISION",
    trace_id: "ra-5244f95c634f",
    verdict: "SUGGEST_REMEDIATION",
    lane: "SYS_RESOURCE",
    timestamp: new Date(Date.now() - 4 * 60 * 60 * 1000).toISOString(),
    hash: "sha256:1b5d4c3a",
    prev_hash: "sha256:0a2e3f4d",
    signed: false,
  },
];

export const MOCK_ACTIVE_TRACES = [
  {
    trace_id: "gw-prom-6d368e6b3025",
    lane: "SYS_HARD_FAIL",
    stage: "PLAN_EMITTED",
    alertname: "HighCPUUsage",
    namespace: "multi-agent",
    workload: "nginx-test",
    age_s: 180,
    verdict: "SUGGEST_REMEDIATION",
  },
  {
    trace_id: "ra-5244f95c634f",
    lane: "SYS_RESOURCE",
    stage: "INGESTED",
    alertname: "PodMemoryHigh",
    namespace: "multi-agent",
    workload: "omni-analyst",
    age_s: 12,
    verdict: null,
  },
  {
    trace_id: "ra-cc7fa4a30761",
    lane: "SYS_HARD_FAIL",
    stage: "DIAGNOSED",
    alertname: "ContainerRestart",
    namespace: "multi-agent",
    workload: "redis",
    age_s: 45,
    verdict: null,
  },
];

export const MOCK_TENANTS = [
  { tenant_id: "default", key_prefix: "omni:tenant:default:****", active_keys: 3, incident_count_24h: 142 },
  { tenant_id: "enterprise", key_prefix: "omni:tenant:enterprise:****", active_keys: 1, incident_count_24h: 28 },
  { tenant_id: "lab", key_prefix: "omni:tenant:lab:****", active_keys: 2, incident_count_24h: 17 },
];

export const MOCK_TELEGRAM_STATUS = {
  chat_id: "-5174042122",
  last_message_id: 2867,
  last_message_at: new Date(Date.now() - 3 * 60 * 1000).toISOString(),
  bot_status: "online",
};
