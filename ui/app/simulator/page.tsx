"use client";

// Admin Simulator — fire a REAL synthetic incident and watch the full pipeline.
//
// Flow: pick a TARGET (Omni system, or a Tenant/Remote-Agent) → SELECT a lane
// (highlights, does not run) → press RUN. Each run injects a synthetic-but-real
// alert/evidence onto the live Kafka topic, so the whole worker flow executes and
// streams back over SSE. Remote-Agent target routes through the remote_agent
// pipeline (evidence_source=RemoteAgent) at critical urgency, so the multi-turn
// diagnosis loop runs and a deep-check session is produced.

import { useEffect, useState } from "react";
import { Sidebar } from "@/components/sidebar";
import { Radio, Play, Loader2, AlertTriangle, Server, Boxes, ChevronRight } from "lucide-react";
import { LANE_TEXT, LANE_BORDER, LANE_DESC, LANE_DOT } from "@/components/shared/lane-tokens";
import {
  useTraceEventStream,
  usePipelineTrace,
  TracePipelineView,
  fmtAgo,
  verdictColor,
  POLL_INTERVAL_MS,
} from "@/components/pipeline/trace-view";
import { DeepCheckPanel } from "@/components/pipeline/deep-check";
import type { SimulateResponse } from "@/app/api/simulate/[lane]/route";
import type { RemoteAgent, RemoteAgentsResponse } from "@/app/api/remote-agents/route";

const LANES: { key: string; laneLabel: string; title: string }[] = [
  { key: "sys_resource", laneLabel: "SYS_RESOURCE", title: "Lane 1 — Resource" },
  { key: "sys_hard_fail", laneLabel: "SYS_HARD_FAIL", title: "Lane 2 — System Hard Fail" },
  { key: "app_http", laneLabel: "APP_HTTP", title: "Lane 3 — App HTTP" },
  { key: "siem_security", laneLabel: "SIEM_SECURITY", title: "Lane 4 — SIEM Security" },
];

type Target = "omni" | "remote";
const SYNTHETIC = "__synthetic__";

interface SimRun {
  lane: string;
  laneLabel: string;
  traceId: string;
  target: Target;
  tenantId: string;
  agentId: string;
  startedAt: number;
  source?: string;
}

function useRemoteAgents() {
  const [agents, setAgents] = useState<RemoteAgent[]>([]);
  useEffect(() => {
    fetch("/api/remote-agents", { cache: "no-store" })
      .then((r) => r.json())
      .then((d: RemoteAgentsResponse) => setAgents(d.agents ?? []))
      .catch(() => setAgents([]));
  }, []);
  return agents;
}

function TargetToggle({ target, onChange }: { target: Target; onChange: (t: Target) => void }) {
  const opts: { key: Target; label: string; icon: typeof Boxes }[] = [
    { key: "omni", label: "Omni system", icon: Boxes },
    { key: "remote", label: "Tenant / Remote Agent", icon: Server },
  ];
  return (
    <div className="flex flex-col gap-1">
      {opts.map((o) => {
        const Icon = o.icon;
        const active = target === o.key;
        return (
          <button
            key={o.key}
            onClick={() => onChange(o.key)}
            className={`flex items-center gap-2 px-2.5 py-1.5 rounded border text-[10px] transition-colors
              ${active ? "border-amber-600/60 bg-amber-950/30 text-amber-300" : "border-zinc-800 text-zinc-500 hover:bg-zinc-900"}`}
          >
            <Icon size={12} /> {o.label}
          </button>
        );
      })}
    </div>
  );
}

export default function SimulatorPage() {
  const [now, setNow] = useState(() => Date.now());
  const [target, setTarget] = useState<Target>("omni");
  const [selectedLane, setSelectedLane] = useState<string>("");
  const [tenantId, setTenantId] = useState<string>("default");
  const [agentSel, setAgentSel] = useState<string>(SYNTHETIC);
  const [runs, setRuns] = useState<SimRun[]>([]);
  const [activeTrace, setActiveTrace] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>("");

  const agents = useRemoteAgents();
  const { connected, eventSeq, lastTraceId } = useTraceEventStream();
  const pipelineSeq = lastTraceId === activeTrace ? eventSeq : 0;
  const { pipeline, loading } = usePipelineTrace(activeTrace, pipelineSeq, connected);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(t);
  }, []);

  // Keep tenant in sync when picking a real agent.
  useEffect(() => {
    if (agentSel !== SYNTHETIC) {
      const a = agents.find((x) => x.agent_id === agentSel);
      const t = (a as unknown as { tenant_id?: string })?.tenant_id;
      if (t) setTenantId(t);
    }
  }, [agentSel, agents]);

  const canRun = !!selectedLane && !busy;

  async function run() {
    if (!selectedLane) return;
    setBusy(true);
    setError("");
    const lane = LANES.find((l) => l.key === selectedLane)!;
    const reqBody =
      target === "remote"
        ? { target, tenant_id: tenantId, agent_id: agentSel === SYNTHETIC ? "" : agentSel }
        : { target };
    try {
      const res = await fetch(`/api/simulate/${lane.key}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(reqBody),
        cache: "no-store",
      });
      if (!res.ok) {
        setError(`Injection failed (${res.status})`);
        return;
      }
      const data = (await res.json()) as SimulateResponse & { tenant_id?: string; agent_id?: string; target?: Target };
      const run: SimRun = {
        lane: lane.key,
        laneLabel: data.lane_label,
        traceId: data.trace_id,
        target: (data.target as Target) ?? target,
        tenantId: data.tenant_id ?? tenantId,
        agentId: data.agent_id ?? "",
        startedAt: Math.floor(Date.now() / 1000),
        source: data.source,
      };
      setRuns((prev) => [run, ...prev].slice(0, 20));
      setActiveTrace(data.trace_id);
    } catch {
      setError("Injection request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full">
      <Sidebar />
      <main className="flex-1 flex flex-col overflow-hidden bg-zinc-950 font-mono text-[11px]">
        <header className="sticky top-0 z-10 flex items-center justify-between px-4 h-9 border-b border-zinc-800 bg-zinc-950 shrink-0">
          <div className="flex items-center gap-3 text-[10px]">
            <span className="text-amber-400 font-semibold tracking-widest uppercase">Simulator</span>
            <span className="text-zinc-700">select → run → watch real pipeline</span>
          </div>
          <div className="flex items-center gap-3 text-[10px]">
            <span className="text-zinc-600">{new Date(now).toLocaleTimeString()}</span>
            <span className={`flex items-center gap-1 ${connected ? "text-emerald-400" : "text-zinc-500"}`}>
              <Radio size={9} className={connected ? "animate-pulse" : ""} />
              {connected ? "live (SSE)" : `polling ${POLL_INTERVAL_MS / 1000}s`}
            </span>
          </div>
        </header>

        <div className="flex flex-1 overflow-hidden">
          {/* LEFT: config + lane select + RUN + history */}
          <aside className="w-80 shrink-0 flex flex-col border-r border-zinc-800 overflow-y-auto">
            {/* Target */}
            <div className="px-3 py-2 border-b border-zinc-800/60">
              <span className="text-[8px] uppercase tracking-widest text-zinc-600">Target</span>
            </div>
            <div className="px-3 py-2">
              <TargetToggle target={target} onChange={setTarget} />
            </div>

            {/* Tenant + agent (remote only) */}
            {target === "remote" && (
              <div className="px-3 pb-2 flex flex-col gap-2">
                <label className="flex flex-col gap-1">
                  <span className="text-[8px] uppercase tracking-widest text-zinc-600">Remote Agent</span>
                  <select
                    value={agentSel}
                    onChange={(e) => setAgentSel(e.target.value)}
                    className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-[10px] text-zinc-300"
                  >
                    <option value={SYNTHETIC}>Synthetic agent (auto)</option>
                    {agents.map((a) => (
                      <option key={a.agent_id} value={a.agent_id}>
                        {a.agent_id} · {a.status}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-[8px] uppercase tracking-widest text-zinc-600">Tenant ID</span>
                  <input
                    value={tenantId}
                    onChange={(e) => setTenantId(e.target.value)}
                    className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-[10px] text-zinc-300 font-mono"
                  />
                </label>
                {agents.length === 0 && (
                  <p className="text-[8px] text-zinc-600">No live agents — synthetic will be used.</p>
                )}
              </div>
            )}

            {/* Lane select */}
            <div className="px-3 py-2 border-y border-zinc-800/60">
              <span className="text-[8px] uppercase tracking-widest text-zinc-600">Select lane</span>
            </div>
            <div className="flex flex-col gap-1.5 p-3">
              {LANES.map((lane) => {
                const active = selectedLane === lane.key;
                return (
                  <button
                    key={lane.key}
                    onClick={() => setSelectedLane(lane.key)}
                    className={`group flex flex-col gap-1 text-left p-2.5 rounded-lg border-l-2 border transition-colors duration-150
                      ${LANE_BORDER[lane.laneLabel] ?? "border-l-zinc-600"}
                      ${active ? "border-amber-600/60 bg-zinc-800/70 ring-1 ring-amber-600/30" : "border-zinc-800 bg-zinc-900/40 hover:bg-zinc-900"}`}
                  >
                    <div className="flex items-center gap-2">
                      <span className={`w-2 h-2 rounded-full ${LANE_DOT[lane.laneLabel] ?? "bg-zinc-500"}`} />
                      <span className={`text-[10px] font-bold tracking-wide uppercase ${LANE_TEXT[lane.laneLabel] ?? "text-zinc-300"}`}>
                        {lane.title}
                      </span>
                      {active && <ChevronRight size={12} className="ml-auto text-amber-400" />}
                    </div>
                    <p className="text-[8px] text-zinc-500 leading-relaxed">{LANE_DESC[lane.laneLabel as keyof typeof LANE_DESC]}</p>
                  </button>
                );
              })}
            </div>

            {/* RUN */}
            <div className="px-3 pb-3">
              <button
                onClick={run}
                disabled={!canRun}
                className={`w-full flex items-center justify-center gap-2 py-2.5 rounded-lg border text-[11px] font-bold uppercase tracking-widest transition-colors
                  ${canRun
                    ? "border-amber-500/60 bg-amber-950/40 text-amber-300 hover:bg-amber-900/40"
                    : "border-zinc-800 bg-zinc-900/40 text-zinc-700 cursor-not-allowed"}`}
              >
                {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                {busy ? "Running…" : selectedLane ? "Run" : "Select a lane"}
              </button>
              {error && (
                <div className="mt-2 flex items-center gap-1.5 text-[9px] text-rose-400 border border-rose-900/60 bg-rose-950/40 rounded px-2 py-1.5">
                  <AlertTriangle size={11} /> {error}
                </div>
              )}
            </div>

            {/* History */}
            <div className="px-3 py-2 border-y border-zinc-800/60">
              <span className="text-[8px] uppercase tracking-widest text-zinc-600">Runs ({runs.length})</span>
            </div>
            {runs.length === 0 ? (
              <div className="flex flex-col items-center justify-center gap-2 text-zinc-700 px-4 py-6 text-center">
                <p className="text-[9px]">No runs yet</p>
              </div>
            ) : (
              <div className="flex flex-col divide-y divide-zinc-800/50">
                {runs.map((r) => (
                  <button
                    key={r.traceId}
                    onClick={() => setActiveTrace(r.traceId)}
                    className={`w-full text-left px-3 py-2 border-l-2 transition-colors duration-150
                      ${r.traceId === activeTrace
                        ? `${LANE_BORDER[r.laneLabel] ?? "border-l-zinc-600"} bg-zinc-800/60`
                        : "border-l-transparent hover:bg-zinc-900/60"}`}
                  >
                    <div className="flex items-center gap-1.5 mb-1">
                      <span className={`w-1.5 h-1.5 rounded-full ${LANE_DOT[r.laneLabel] ?? "bg-zinc-500"}`} />
                      <span className={`text-[9px] font-bold tracking-widest uppercase ${LANE_TEXT[r.laneLabel] ?? "text-zinc-400"}`}>
                        {r.laneLabel}
                      </span>
                      <span className="ml-auto text-[7px] text-zinc-600 uppercase">{r.target === "remote" ? "remote" : "omni"}</span>
                    </div>
                    <div className="text-[9px] text-zinc-400 font-mono truncate">{r.traceId}</div>
                    <div className="flex items-center justify-between mt-0.5">
                      {r.target === "remote" ? (
                        <span className="text-[8px] text-zinc-600 truncate">{r.tenantId}/{r.agentId || "synthetic"}</span>
                      ) : <span />}
                      <span className="text-[8px] text-zinc-700">{fmtAgo(r.startedAt)}</span>
                    </div>
                  </button>
                ))}
              </div>
            )}
          </aside>

          {/* CENTER: live pipeline */}
          <section className="flex-1 flex flex-col overflow-auto" aria-label="Simulator pipeline">
            {!activeTrace ? (
              <div className="flex flex-col items-center justify-center flex-1 gap-3 text-zinc-700 text-center px-6">
                <Play size={22} className="opacity-30" />
                <p className="text-[11px] text-zinc-500">Pick a target, select a lane, then press Run</p>
                <p className="text-[9px] max-w-sm leading-relaxed">
                  Omni target injects an in-cluster alert. Tenant/Remote-Agent target injects
                  evidence as a remote agent at critical urgency — running the deep multi-turn
                  diagnosis loop. The full pipeline streams here.
                </p>
              </div>
            ) : (
              <>
                {pipeline?.verdict && (
                  <div className="px-4 py-1.5 border-b border-zinc-800 bg-zinc-950/60 text-[9px] flex items-center gap-2">
                    <span className="text-zinc-600 uppercase tracking-widest">verdict</span>
                    <span className={`font-semibold ${verdictColor(pipeline.verdict)}`}>{pipeline.verdict}</span>
                  </div>
                )}
                <TracePipelineView pipeline={pipeline} loading={loading} />
                <DeepCheckPanel traceId={activeTrace} liveSeq={eventSeq} />
              </>
            )}
          </section>
        </div>
      </main>
    </div>
  );
}
