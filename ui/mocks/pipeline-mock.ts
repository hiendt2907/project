// Pipeline mock — 3 traces (one per lane) with varied stage statuses

export type PipelineStageStatus = "ok" | "fail" | "skip" | "pending";
export type PipelineStage =
  | "INGEST"
  | "EVIDENCE"
  | "RAG"
  | "LLM"
  | "SCHEMA"
  | "KILLSWITCH"
  | "CRAT"
  | "DISPATCH"
  | "HITL"
  | "EXECUTOR"
  | "FEEDBACK";

export const PIPELINE_STAGES: PipelineStage[] = [
  "INGEST",
  "EVIDENCE",
  "RAG",
  "LLM",
  "SCHEMA",
  "KILLSWITCH",
  "CRAT",
  "DISPATCH",
  "HITL",
  "EXECUTOR",
  "FEEDBACK",
];

export interface PipelineStageEntry {
  stage: PipelineStage;
  status: PipelineStageStatus;
  ts: number;
  detail: string;
  elapsed_ms: number;
}

export interface PipelineTrace {
  found: boolean;
  trace_id: string;
  lane: string;
  started_at: number;
  updated_at: number;
  verdict: string;
  stages: PipelineStageEntry[];
}

const NOW = Date.now() / 1000;

export const MOCK_PIPELINE_SIEM: PipelineTrace = {
  found: true,
  trace_id: "chaos-siem-a1b2c3d4",
  lane: "SIEM_SECURITY",
  started_at: NOW - 92,
  updated_at: NOW - 4,
  verdict: "SUGGEST_REMEDIATION",
  stages: [
    { stage: "INGEST", status: "ok", ts: NOW - 92, detail: "kafka omni-alerts offset=8821 partition=2", elapsed_ms: 12 },
    { stage: "EVIDENCE", status: "ok", ts: NOW - 90, detail: "siem_evidence_raw: 14 events, entity_count=6", elapsed_ms: 210 },
    { stage: "RAG", status: "skip", ts: NOW - 88, detail: "recall=0.82 — cache hit, skipped full embed scan", elapsed_ms: 34 },
    { stage: "LLM", status: "ok", ts: NOW - 85, detail: "qwen2.5-coder:7b num_ctx=8192 predict=1024 duration=18.4s", elapsed_ms: 18400 },
    { stage: "SCHEMA", status: "ok", ts: NOW - 66, detail: "AnalystAdvisory parsed: root_cause=DDoS kill-chain", elapsed_ms: 8 },
    { stage: "KILLSWITCH", status: "ok", ts: NOW - 65, detail: "OMNI_AUTO_EXECUTE_ENABLED=false → advisory mode", elapsed_ms: 1 },
    { stage: "CRAT", status: "ok", ts: NOW - 64, detail: "block#4823 sha256:9f3d2a8b prev:7c2e1b9a signed=false", elapsed_ms: 42 },
    { stage: "DISPATCH", status: "ok", ts: NOW - 60, detail: "SUGGEST_REMEDIATION → kafka omni-actions + telegram", elapsed_ms: 88 },
    { stage: "HITL", status: "skip", ts: NOW - 58, detail: "advisory mode — HITL not required", elapsed_ms: 0 },
    { stage: "EXECUTOR", status: "skip", ts: NOW - 57, detail: "kill-switch active — no mutation", elapsed_ms: 0 },
    { stage: "FEEDBACK", status: "ok", ts: NOW - 4, detail: "outcome=accepted tenant=default", elapsed_ms: 6 },
  ],
};

export const MOCK_PIPELINE_RESOURCE: PipelineTrace = {
  found: true,
  trace_id: "gw-prom-6d368e6b3025",
  lane: "SYS_RESOURCE",
  started_at: NOW - 210,
  updated_at: NOW - 180,
  verdict: "SUGGEST_REMEDIATION",
  stages: [
    { stage: "INGEST", status: "ok", ts: NOW - 210, detail: "kafka omni-diagnostic-evidence offset=5302", elapsed_ms: 9 },
    { stage: "EVIDENCE", status: "ok", ts: NOW - 208, detail: "baseline z_cpu=4.12 z_mem=1.3 → 3σ gate PASS", elapsed_ms: 135 },
    { stage: "RAG", status: "ok", ts: NOW - 206, detail: "recall=0.91 top-3 SOP hits, cosine>0.82", elapsed_ms: 310 },
    { stage: "LLM", status: "ok", ts: NOW - 203, detail: "qwen2.5-coder:7b duration=22.1s num_predict=1024", elapsed_ms: 22100 },
    { stage: "SCHEMA", status: "ok", ts: NOW - 181, detail: "AnalystAdvisory root_cause=CPU spike nginx-test", elapsed_ms: 11 },
    { stage: "KILLSWITCH", status: "ok", ts: NOW - 181, detail: "OMNI_AUTO_EXECUTE_ENABLED=false → suggest only", elapsed_ms: 1 },
    { stage: "CRAT", status: "ok", ts: NOW - 180, detail: "block#4818 sha256:ab12cd34 prev:ef56gh78 signed=false", elapsed_ms: 38 },
    { stage: "DISPATCH", status: "ok", ts: NOW - 180, detail: "SUGGEST_REMEDIATION dispatched to telegram", elapsed_ms: 64 },
    { stage: "HITL", status: "skip", ts: NOW - 180, detail: "advisory mode active", elapsed_ms: 0 },
    { stage: "EXECUTOR", status: "skip", ts: NOW - 180, detail: "kill-switch active", elapsed_ms: 0 },
    { stage: "FEEDBACK", status: "pending", ts: NOW, detail: "awaiting operator feedback", elapsed_ms: 0 },
  ],
};

export const MOCK_PIPELINE_HARDFAIL: PipelineTrace = {
  found: true,
  trace_id: "gw-prom-5f8d67832713",
  lane: "SYS_HARD_FAIL",
  started_at: NOW - 38,
  updated_at: NOW - 6,
  verdict: "HITL_PENDING",
  stages: [
    { stage: "INGEST", status: "ok", ts: NOW - 38, detail: "kafka omni-diagnostic-evidence offset=5419", elapsed_ms: 11 },
    { stage: "EVIDENCE", status: "ok", ts: NOW - 36, detail: "OS_STATE_CONTRAST: systemd probe conflict PASSED vs SYS_HARD_FAIL", elapsed_ms: 88 },
    { stage: "RAG", status: "ok", ts: NOW - 35, detail: "recall=0.87 sys_hard_fail_os_advisory_pairs hit", elapsed_ms: 290 },
    { stage: "LLM", status: "ok", ts: NOW - 32, detail: "qwen2.5-coder:7b duration=19.8s schema=AnalystAdvisory", elapsed_ms: 19800 },
    { stage: "SCHEMA", status: "ok", ts: NOW - 12, detail: "approval_required=true — ConfigMap missing spec-break", elapsed_ms: 7 },
    { stage: "KILLSWITCH", status: "ok", ts: NOW - 12, detail: "OMNI_AUTO_EXECUTE_ENABLED=false → HITL escalation", elapsed_ms: 1 },
    { stage: "CRAT", status: "ok", ts: NOW - 11, detail: "block#4820 sha256:7c2e1b9a prev:3f1e9c2d signed=false", elapsed_ms: 41 },
    { stage: "DISPATCH", status: "ok", ts: NOW - 10, detail: "HITL_PENDING → kafka omni-hitl-pending", elapsed_ms: 72 },
    { stage: "HITL", status: "pending", ts: NOW - 6, detail: "awaiting FinGuard HITL API approval — timeout 900s", elapsed_ms: 0 },
    { stage: "EXECUTOR", status: "pending", ts: NOW, detail: "blocked on HITL decision", elapsed_ms: 0 },
    { stage: "FEEDBACK", status: "pending", ts: NOW, detail: "blocked on HITL decision", elapsed_ms: 0 },
  ],
};

export const MOCK_PIPELINE_TRACES: PipelineTrace[] = [
  MOCK_PIPELINE_SIEM,
  MOCK_PIPELINE_HARDFAIL,
  MOCK_PIPELINE_RESOURCE,
];

export interface RecentTrace {
  trace_id: string;
  lane: string;
  current_stage: PipelineStage;
  verdict: string;
  started_at: number;
  updated_at: number;
}

export const MOCK_RECENT_TRACES: RecentTrace[] = MOCK_PIPELINE_TRACES.map((t) => {
  const lastOk = [...t.stages].reverse().find((s) => s.status === "ok" || s.status === "pending");
  return {
    trace_id: t.trace_id,
    lane: t.lane,
    current_stage: (lastOk?.stage ?? "INGEST") as PipelineStage,
    verdict: t.verdict,
    started_at: t.started_at,
    updated_at: t.updated_at,
  };
});
