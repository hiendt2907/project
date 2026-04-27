import { NextResponse } from "next/server";

const LEVELS = ["error", "critical", "warning"] as const;
const WORKERS = ["evidence_consumer", "analyst_agentic_loop", "diagnostic_dispatcher", "siem_bridge", "kubectl_cluster", "reasoning_evidence_inbound"];

const entries = Array.from({ length: 40 }, (_, i) => {
  const level = LEVELS[i % 3];
  const worker = WORKERS[i % WORKERS.length];
  const ts = new Date(Date.now() - i * 3 * 60 * 1000).toISOString();
  const msgs: Record<string, string[]> = {
    error: [
      "RAG query failed: index not found for collection k8s_expert",
      "LLM response parse error: unexpected token in JSON at position 0",
      "Redis HSET timeout after 5000ms on key doc:sop_runbooks:abc123",
      "Kafka consumer group lag exceeded threshold: omni-actions:12000",
    ],
    critical: [
      "SIEM incident unrouted — no playbook match for category=network_threat severity=critical",
      "Executor gate BLOCKED: mutate attempted outside lab mode",
      "Evidence gate violation: EXECUTE_MUTATE without HITL approval trace",
    ],
    warning: [
      "Semantic cache miss ratio >40% in last 5min — consider warming cache",
      "Pod omni-analyst restarted (OOMKill): increased memory to 768Mi",
      "Playbook pb-002 action cordon_node skipped: node already cordoned",
    ],
  };
  const msgList = msgs[level];
  return {
    id: `err-${String(i + 1).padStart(4, "0")}`,
    level,
    worker,
    message: msgList[i % msgList.length],
    trace_id: `tr-${Math.random().toString(36).slice(2, 10)}`,
    timestamp: ts,
    ttl_remaining_s: Math.max(0, 3600 - i * 90),
  };
});

export async function GET() {
  return NextResponse.json({ entries, total: entries.length });
}
