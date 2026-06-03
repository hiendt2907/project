// PipelineFlow — T1 visual of the end-to-end Omni workflow.
// Node health is DERIVED client-side from worker roles + kafka lag (no backend
// endpoint needed). Purely compositor-friendly styling (no layout animation).

export interface PipelineWorker {
  role: string;
  status: "healthy" | "degraded" | "unhealthy";
}

export interface PipelineEdgeLag {
  topic: string;
  lag: number;
}

type NodeKind = "source" | "kafka" | "worker" | "sink";

interface FlowNode {
  id: string;
  label: string;
  kind: NodeKind;
  role?: string; // maps to a worker role for health
  topic?: string; // maps to a kafka topic for lag
  detail: string;
}

// Fixed topology mirroring CLAUDE.md End-to-End Pipeline.
const NODES: FlowNode[] = [
  { id: "finguard", label: "FinGuard", kind: "source", detail: "stream:actionable_incidents" },
  { id: "bridge", label: "siem-bridge", kind: "worker", role: "siem-bridge", detail: "Redis XREADGROUP → kafka" },
  { id: "alerts", label: "omni-alerts", kind: "kafka", topic: "omni-alerts", detail: "kafka topic" },
  { id: "prober", label: "prober", kind: "worker", role: "prober", detail: "diagnostic pipeline + temporal" },
  { id: "evidence", label: "omni-diagnostic-evidence", kind: "kafka", topic: "omni-diagnostic-evidence", detail: "kafka topic" },
  { id: "analyst", label: "analyst", kind: "worker", role: "analyst", detail: "RAG gate → LLM → advisory" },
  { id: "actions", label: "omni-actions", kind: "kafka", topic: "omni-actions", detail: "kafka topic" },
  { id: "executor", label: "executor", kind: "worker", role: "executor", detail: "remediation (mutate only)" },
  { id: "feedback", label: "omni-action-feedback", kind: "kafka", topic: "omni-action-feedback", detail: "→ analyst re-eval" },
];

const KIND_ACCENT: Record<NodeKind, string> = {
  source: "border-sky-500/40 bg-sky-500/5",
  kafka: "border-zinc-700 bg-zinc-900/60",
  worker: "border-amber-500/30 bg-amber-500/5",
  sink: "border-emerald-500/40 bg-emerald-500/5",
};

function statusDot(status: "healthy" | "degraded" | "unhealthy" | "unknown"): string {
  if (status === "healthy") return "bg-emerald-400";
  if (status === "degraded") return "bg-amber-400";
  if (status === "unhealthy") return "bg-rose-400";
  return "bg-zinc-600";
}

function lagColor(lag: number): string {
  if (lag >= 1000) return "text-rose-400";
  if (lag >= 100) return "text-amber-400";
  return "text-zinc-500";
}

interface PipelineFlowProps {
  workers: PipelineWorker[];
  kafkaLag: PipelineEdgeLag[];
}

export function PipelineFlow({ workers, kafkaLag }: PipelineFlowProps) {
  const roleStatus = new Map(workers.map((w) => [w.role, w.status]));
  const topicLag = new Map(kafkaLag.map((k) => [k.topic, k.lag]));
  // Roles may be collapsed into a single "full" monolith pod.
  const fullStatus = roleStatus.get("full");

  function workerStatus(role: string): "healthy" | "degraded" | "unhealthy" | "unknown" {
    return roleStatus.get(role) ?? fullStatus ?? "unknown";
  }

  return (
    <div className="overflow-x-auto pb-1">
      <div className="flex items-stretch gap-1 min-w-max">
        {NODES.map((node, i) => {
          const isLast = i === NODES.length - 1;
          const lag = node.topic ? topicLag.get(node.topic) : undefined;
          const wStatus = node.role ? workerStatus(node.role) : null;
          return (
            <div key={node.id} className="flex items-stretch gap-1">
              <div
                title={node.detail}
                className={`group relative w-[112px] shrink-0 rounded border px-2 py-1.5 transition-colors hover:border-amber-400/60 ${KIND_ACCENT[node.kind]}`}
              >
                <div className="flex items-center gap-1.5">
                  {wStatus && <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusDot(wStatus)}`} />}
                  <span className="text-[10px] text-zinc-200 truncate font-medium">{node.label}</span>
                </div>
                <p className="text-[8px] text-zinc-600 truncate mt-0.5 group-hover:text-zinc-500">
                  {node.detail}
                </p>
                {node.kind === "kafka" && (
                  <p className={`text-[8px] tabular-nums mt-0.5 ${lag !== undefined ? lagColor(lag) : "text-zinc-700"}`}>
                    lag {lag !== undefined ? lag : "—"}
                  </p>
                )}
              </div>
              {!isLast && (
                <div className="flex items-center text-zinc-700 text-[10px] shrink-0">→</div>
              )}
            </div>
          );
        })}
      </div>
      {/* Branches */}
      <div className="flex items-center gap-3 mt-2 pl-1 text-[8px] text-zinc-600">
        <span>↳ branches:</span>
        <span className="text-orange-400">analyst → omni-hitl-pending → HITL API</span>
        <span className="text-sky-400">analyst → Telegram advisory</span>
        <span className="text-violet-400">analyst → omni-audit-chain (CRAT)</span>
      </div>
    </div>
  );
}
