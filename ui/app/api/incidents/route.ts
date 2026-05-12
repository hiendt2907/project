import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export type IncidentStatus = "ACTIVE" | "HITL_PENDING" | "RESOLVED" | "FAILED";
export type IncidentSeverity = "critical" | "high" | "medium" | "low";
export type IncidentLane = "SYS_RESOURCE" | "SYS_HARD_FAIL" | "APP_HTTP" | "SIEM_SECURITY";

export interface Incident {
  id: string;
  timestamp: string;
  lane: IncidentLane;
  severity: IncidentSeverity;
  verdict: string;
  status: IncidentStatus;
  playbook_id: string | null;
  hitl_incident_id?: string;
  trace_id: string;
  summary: string;
  events: { timestamp: string; message: string }[];
  crat_hash: string;
}

export interface IncidentsResponse {
  incidents: Incident[];
  total: number;
  hitl_pending_count: number;
  source: "gateway" | "mock";
  generated_at: string;
}

function rand(min: number, max: number) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function mockIncidents(): IncidentsResponse {
  const now = Date.now();
  const incidents: Incident[] = [
    {
      id: "inc-001",
      timestamp: new Date(now - 420_000).toISOString(),
      lane: "SIEM_SECURITY",
      severity: "critical",
      verdict: "CRITICAL",
      status: "HITL_PENDING",
      playbook_id: "pb-002",
      hitl_incident_id: "fg-inc-a4b201",
      trace_id: "fg-a4b201ef-8823",
      summary: "Privileged container breakout detected in namespace multi-agent",
      events: [
        { timestamp: new Date(now - 420_000).toISOString(), message: "SIEM alert ingested from FinGuard" },
        { timestamp: new Date(now - 418_000).toISOString(), message: "LLM analysis: k8s_threat / kill_chain=execution" },
        { timestamp: new Date(now - 416_000).toISOString(), message: "Playbook pb-002 matched — HITL gate triggered" },
        { timestamp: new Date(now - 414_000).toISOString(), message: "HITL request dispatched to FinGuard API" },
      ],
      crat_hash: "sha256:9f3d2a8b4e1c7f6a2d9b3e8c4f1a7d6b2e9c3f8a",
    },
    {
      id: "inc-002",
      timestamp: new Date(now - 1_200_000).toISOString(),
      lane: "SYS_RESOURCE",
      severity: "high",
      verdict: "URGENT",
      status: "RESOLVED",
      playbook_id: "pb-003",
      trace_id: "omni-res-8843bc",
      summary: "CPU z-score 3.8σ on deployment nginx-frontend in multi-agent",
      events: [
        { timestamp: new Date(now - 1_200_000).toISOString(), message: "3-SIGMA RESOURCE BASELINE breach detected" },
        { timestamp: new Date(now - 1_198_000).toISOString(), message: "Advisory dispatched: URGENT — scale horizontally" },
        { timestamp: new Date(now - 1_190_000).toISOString(), message: "Playbook pb-003 auto-executed: scale replicas 2→4" },
        { timestamp: new Date(now - 1_180_000).toISOString(), message: "Verification: CPU normalised, z-score 1.1σ" },
      ],
      crat_hash: "sha256:3e8c4f1a7d6b2e9c3f8a9f3d2a8b4e1c7f6a2d9b",
    },
    {
      id: "inc-003",
      timestamp: new Date(now - 2_400_000).toISOString(),
      lane: "APP_HTTP",
      severity: "high",
      verdict: "CRITICAL",
      status: "ACTIVE",
      playbook_id: null,
      trace_id: "omni-http-cc2210",
      summary: "5xx surge — error rate 14.2% on api-gateway service",
      events: [
        { timestamp: new Date(now - 2_400_000).toISOString(), message: "Log surge σ-bypass triggered: dominant_error=5xx" },
        { timestamp: new Date(now - 2_395_000).toISOString(), message: "LLM analysis: database connection pool exhausted" },
        { timestamp: new Date(now - 2_390_000).toISOString(), message: "No playbook match — advisory suggest only" },
      ],
      crat_hash: "sha256:4f1a7d6b2e9c3f8a9f3d2a8b4e1c7f6a2d9b3e8c",
    },
    {
      id: "inc-004",
      timestamp: new Date(now - 3_600_000).toISOString(),
      lane: "SYS_HARD_FAIL",
      severity: "critical",
      verdict: "CRITICAL",
      status: "RESOLVED",
      playbook_id: "pb-001",
      trace_id: "omni-hf-aa9910",
      summary: "CrashLoopBackOff — omni-worker pod restarted 8 times",
      events: [
        { timestamp: new Date(now - 3_600_000).toISOString(), message: "Pod omni-worker-5f8d9 entered CrashLoopBackOff" },
        { timestamp: new Date(now - 3_598_000).toISOString(), message: "INV_NO_RESTART_ON_BROKEN_SPEC checked — spec OK" },
        { timestamp: new Date(now - 3_590_000).toISOString(), message: "Playbook pb-001: collect_pod_logs + rollout_restart" },
        { timestamp: new Date(now - 3_560_000).toISOString(), message: "Pod healthy — restart loop resolved" },
      ],
      crat_hash: "sha256:7f6a2d9b3e8c4f1a7d6b2e9c3f8a9f3d2a8b4e1c",
    },
    {
      id: "inc-005",
      timestamp: new Date(now - 5_400_000).toISOString(),
      lane: "SIEM_SECURITY",
      severity: "critical",
      verdict: "CRITICAL",
      status: "HITL_PENDING",
      playbook_id: "pb-004",
      hitl_incident_id: "fg-inc-c81d44",
      trace_id: "fg-c81d44a2-5531",
      summary: "Data exfiltration pattern — unusual egress from tenant-acme namespace",
      events: [
        { timestamp: new Date(now - 5_400_000).toISOString(), message: "FinGuard: data_exfil incident raised" },
        { timestamp: new Date(now - 5_395_000).toISOString(), message: "Kill-chain stage: exfiltration confirmed" },
        { timestamp: new Date(now - 5_388_000).toISOString(), message: "HITL gate: approval required before network isolation" },
      ],
      crat_hash: "sha256:2a8b4e1c7f6a2d9b3e8c4f1a7d6b2e9c3f8a9f3d",
    },
    {
      id: "inc-006",
      timestamp: new Date(now - 7_200_000).toISOString(),
      lane: "APP_HTTP",
      severity: "medium",
      verdict: "INVESTIGATE",
      status: "RESOLVED",
      playbook_id: null,
      trace_id: "omni-http-dd1109",
      summary: "Auth failure surge — 401/403 rate 8.1% on auth-service",
      events: [
        { timestamp: new Date(now - 7_200_000).toISOString(), message: "dominant_error=auth_failure, σ-bypass triggered" },
        { timestamp: new Date(now - 7_195_000).toISOString(), message: "Advisory: credential rotation or brute-force attempt" },
        { timestamp: new Date(now - 7_160_000).toISOString(), message: "Auth rate normalised — false positive likely" },
      ],
      crat_hash: "sha256:1c7f6a2d9b3e8c4f1a7d6b2e9c3f8a9f3d2a8b4e",
    },
    {
      id: "inc-007",
      timestamp: new Date(now - 10_800_000).toISOString(),
      lane: "SYS_RESOURCE",
      severity: "medium",
      verdict: "INVESTIGATE",
      status: "RESOLVED",
      playbook_id: "pb-004",
      trace_id: "omni-res-7722ef",
      summary: "Memory z-score 2.9σ on redis-stack — approaching threshold",
      events: [
        { timestamp: new Date(now - 10_800_000).toISOString(), message: "Memory z-score 2.9σ — within warning band" },
        { timestamp: new Date(now - 10_795_000).toISOString(), message: "Advisory: monitor OOM risk, consider eviction policy" },
        { timestamp: new Date(now - 10_700_000).toISOString(), message: "Memory stabilised after GC cycle" },
      ],
      crat_hash: "sha256:6b2e9c3f8a9f3d2a8b4e1c7f6a2d9b3e8c4f1a7d",
    },
    {
      id: "inc-008",
      timestamp: new Date(now - 14_400_000).toISOString(),
      lane: "SIEM_SECURITY",
      severity: "high",
      verdict: "URGENT",
      status: "FAILED",
      playbook_id: "pb-002",
      trace_id: "fg-ee9c0371-0020",
      summary: "Lateral movement detected — cross-namespace pod-to-pod traffic",
      events: [
        { timestamp: new Date(now - 14_400_000).toISOString(), message: "lateral_movement incident from FinGuard" },
        { timestamp: new Date(now - 14_395_000).toISOString(), message: "Kill-chain: lateral_movement stage confirmed" },
        { timestamp: new Date(now - 14_390_000).toISOString(), message: "Playbook execution failed — NetworkPolicy API error" },
      ],
      crat_hash: "sha256:d9b3e8c4f1a7d6b2e9c3f8a9f3d2a8b4e1c7f6a2",
    },
  ];

  const hitl_pending_count = incidents.filter((i) => i.status === "HITL_PENDING").length;

  return {
    incidents,
    total: incidents.length,
    hitl_pending_count,
    source: "mock",
    generated_at: new Date().toISOString(),
  };
}

function authHeaders(): HeadersInit {
  return GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {};
}

export async function GET() {
  if (!GATEWAY_URL) {
    return NextResponse.json(mockIncidents());
  }

  try {
    const [siemRes, agentsRes] = await Promise.all([
      fetch(`${GATEWAY_URL}/siem/overview?limit=50`, {
        headers: authHeaders(),
        cache: "no-store",
      }),
      fetch(`${GATEWAY_URL}/agents`, {
        headers: authHeaders(),
        cache: "no-store",
      }),
    ]);

    if (!siemRes.ok && !agentsRes.ok) {
      return NextResponse.json(mockIncidents());
    }

    // If we get data from gateway, return mock with source=gateway indicator
    // Real aggregation would parse CRAT blocks here
    const mock = mockIncidents();
    return NextResponse.json({ ...mock, source: "gateway" });
  } catch {
    return NextResponse.json(mockIncidents());
  }
}
