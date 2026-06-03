// Operator Console — mock data focused on active incidents

export type OperatorLane = "SYS_RESOURCE" | "SYS_HARD_FAIL" | "APP_HTTP" | "SIEM_SECURITY";
export type OperatorSeverity = "critical" | "high" | "medium" | "low";
export type OperatorStatus = "ACTIVE" | "HITL_PENDING" | "RESOLVED" | "SUGGEST_ONLY";

export interface OperatorIncident {
  id: string;
  trace_id: string;
  lane: OperatorLane;
  severity: OperatorSeverity;
  status: OperatorStatus;
  alertname: string;
  namespace: string;
  workload: string;
  timestamp: string;
  age_s: number;
  root_cause: string;
  verification_steps: { layer: string; command: string; rationale: string }[];
  suggested_action: string;
  hitl_id?: string;
}

export interface OperatorHITLItem {
  hitl_id: string;
  trace_id: string;
  lane: OperatorLane;
  severity: OperatorSeverity;
  alertname: string;
  namespace: string;
  requested_action: string;
  requested_at: string;
  timeout_at: string;
  approval_required_reason: string;
}

export const MOCK_ACTIVE_INCIDENTS: OperatorIncident[] = [
  {
    id: "inc-001",
    trace_id: "e2e-siem-03234668",
    lane: "SIEM_SECURITY",
    severity: "critical",
    status: "SUGGEST_ONLY",
    alertname: "SIEMDDoSDetected",
    namespace: "multi-agent",
    workload: "omni-gateway",
    timestamp: new Date(Date.now() - 47 * 60 * 1000).toISOString(),
    age_s: 47 * 60,
    root_cause: "DDoS attack from 203.0.113.42: 8420 req/60s exceeds gateway capacity. Kill-chain: recon→volume_flood.",
    verification_steps: [
      { layer: "L4", command: "kubectl logs -n multi-agent -l app=omni-gateway --tail=100 | grep 'rate_limit\\|429'", rationale: "Confirm rate limit events" },
      { layer: "L3", command: "kubectl exec -n multi-agent redis-0 -- redis-cli KEYS 'omni:rate:*' | wc -l", rationale: "Rate limit bucket saturation" },
    ],
    suggested_action: "Block attacker IP at ingress; scale gateway replicas",
  },
  {
    id: "inc-002",
    trace_id: "gw-prom-6d368e6b3025",
    lane: "SYS_HARD_FAIL",
    severity: "high",
    status: "ACTIVE",
    alertname: "HighCPUUsage",
    namespace: "multi-agent",
    workload: "nginx-test",
    timestamp: new Date(Date.now() - 3 * 60 * 1000).toISOString(),
    age_s: 3 * 60,
    root_cause: "nginx-test CPU ~90% saturation for 5m (153394n cores). Container nginx reports 90% vs 50m limit.",
    verification_steps: [
      { layer: "L3", command: "kubectl top pod -n multi-agent nginx-test-7c886d4485-ph7rv", rationale: "Current CPU usage" },
      { layer: "L4", command: "sum(rate(container_cpu_usage_seconds_total{namespace='multi-agent',pod=~'^nginx-test-.*'}[5m]))", rationale: "PromQL CPU rate" },
    ],
    suggested_action: "Review nginx-test load; check if stress test still running",
  },
  {
    id: "inc-003",
    trace_id: "gw-prom-5f8d67832713",
    lane: "SYS_HARD_FAIL",
    severity: "critical",
    status: "HITL_PENDING",
    alertname: "NginxConfigMapMissing",
    namespace: "multi-agent",
    workload: "nginx-deployment",
    timestamp: new Date(Date.now() - 25 * 60 * 1000).toISOString(),
    age_s: 25 * 60,
    root_cause: "Pod CrashLoopBackOff: ConfigMap 'nginx-config' not found. INV_NO_RESTART_ON_BROKEN_SPEC.",
    verification_steps: [
      { layer: "L3", command: "kubectl get configmap nginx-config -n multi-agent", rationale: "Confirm ConfigMap missing" },
      { layer: "L3", command: "kubectl describe pod -n multi-agent | grep -A10 Events", rationale: "Mount error detail" },
    ],
    suggested_action: "Create missing ConfigMap — requires HITL approval (mutation gate)",
    hitl_id: "fg-inc-a4b201",
  },
  {
    id: "inc-004",
    trace_id: "ra-8546605ae9a2",
    lane: "SYS_RESOURCE",
    severity: "medium",
    status: "ACTIVE",
    alertname: "KafkaConsumerLag",
    namespace: "multi-agent",
    workload: "omni-analyst",
    timestamp: new Date(Date.now() - 8 * 60 * 1000).toISOString(),
    age_s: 8 * 60,
    root_cause: "Kafka consumer lag on omni-diagnostic-evidence growing. LLM inference p99=45s causing backpressure.",
    verification_steps: [
      { layer: "L4", command: "kubectl exec -n multi-agent kafka-685dc55dfb-68sz9 -- kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --group omni-analyst-evidence", rationale: "Lag per partition" },
    ],
    suggested_action: "Monitor lag trend; scale omni-analyst if lag > 1000",
  },
];

export const MOCK_HITL_QUEUE: OperatorHITLItem[] = [
  {
    hitl_id: "fg-inc-a4b201",
    trace_id: "gw-prom-5f8d67832713",
    lane: "SYS_HARD_FAIL",
    severity: "critical",
    alertname: "NginxConfigMapMissing",
    namespace: "multi-agent",
    requested_action: "kubectl create configmap nginx-config --from-file=config/ -n multi-agent",
    requested_at: new Date(Date.now() - 20 * 60 * 1000).toISOString(),
    timeout_at: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
    approval_required_reason: "ConfigMap creation in production namespace requires human verification (INV_READ_BEFORE_MUTATE)",
  },
];

export const MOCK_TELEGRAM = {
  chat_id: "-5174042122",
  bot_status: "online" as const,
  last_message: {
    message_id: 2867,
    timestamp: new Date(Date.now() - 3 * 60 * 1000).toISOString(),
    trace_id: "gw-prom-6d368e6b3025",
    text_preview: "🔍 SUGGEST_REMEDIATION — HighCPUUsage nginx-test/multi-agent. Root cause: CPU 90% saturation...",
  },
};
