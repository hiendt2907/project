"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { Sidebar } from "@/components/sidebar";
import { Radio, ChevronRight, AlertTriangle, Shield, CheckCircle2, Clock, XCircle, SkipForward } from "lucide-react";
import { LANE_TEXT, LANE_BORDER, LANE_LABEL, LANE_DOT } from "@/components/shared/lane-tokens";
import { SectionLabel } from "@/components/shared/primitives";
import type { RecentTrace } from "@/app/api/trace/recent/route";
import type { PipelineResponse, PipelineStageEntry, PipelineStageStatus, PipelineStage } from "@/app/api/trace/[id]/pipeline/route";

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

const STAGE_DESCRIPTIONS: Record<string, string> = {
  INGEST:      "Kafka consumer — omni-alerts / omni-diagnostic-evidence",
  EVIDENCE:    "Evidence collection — OS state, 3σ baseline, SIEM events",
  RAG:         "Vector retrieval — HNSW cosine recall against omni:rag:sop",
  LLM:         "Ollama advisory — qwen2.5-coder:7b AnalystAdvisory schema",
  SCHEMA:      "Schema validation — Pydantic AnalystAdvisory parse + repair",
  KILLSWITCH:  "Kill-switch gate — OMNI_AUTO_EXECUTE_ENABLED check",
  CRAT:        "Audit ledger — SHA-256 chain + Ed25519 sign, Kafka emit",
  DISPATCH:    "Action dispatch — Telegram + kafka omni-actions / hitl-pending",
  HITL:        "Human-in-the-loop — FinGuard HITL API approval / rejection",
  EXECUTOR:    "Mutation executor — kubectl via omni-executor (RBAC-scoped)",
  FEEDBACK:    "Feedback loop — omni-action-feedback → KPI ZSet update",
};

const STAGE_GROUPS: Record<string, string> = {
  INGEST: "Ingestion",
  EVIDENCE: "Ingestion",
  RAG: "Analysis",
  LLM: "Analysis",
  SCHEMA: "Analysis",
  KILLSWITCH: "Governance",
  CRAT: "Governance",
  DISPATCH: "Action",
  HITL: "Action",
  EXECUTOR: "Action",
  FEEDBACK: "Action",
};

const POLL_INTERVAL_MS = 3000;
// Slower poll used purely as a safety net once the SSE event stream is connected.
const FALLBACK_POLL_MS = 15000;

// ─────────────────────────────────────────────────────────────────────────────
// Status helpers
// ─────────────────────────────────────────────────────────────────────────────

function statusColor(status: PipelineStageStatus): string {
  switch (status) {
    case "ok":      return "text-emerald-400";
    case "fail":    return "text-rose-400";
    case "skip":    return "text-amber-400";
    case "pending": return "text-zinc-500";
  }
}

function statusBg(status: PipelineStageStatus): string {
  switch (status) {
    case "ok":      return "bg-emerald-950/60 border-emerald-700/60 shadow-emerald-900/30";
    case "fail":    return "bg-rose-950/60 border-rose-700/60 shadow-rose-900/30";
    case "skip":    return "bg-amber-950/40 border-amber-700/40 shadow-amber-900/20";
    case "pending": return "bg-zinc-900/60 border-zinc-700/40";
  }
}

function statusRing(status: PipelineStageStatus): string {
  switch (status) {
    case "ok":      return "ring-1 ring-emerald-600/40";
    case "fail":    return "ring-1 ring-rose-600/40";
    case "skip":    return "ring-1 ring-amber-600/30";
    case "pending": return "";
  }
}

function connectorColor(status: PipelineStageStatus): string {
  switch (status) {
    case "ok":      return "#10b981"; // emerald-500
    case "fail":    return "#f43f5e"; // rose-500
    case "skip":    return "#f59e0b"; // amber-500
    case "pending": return "#3f3f46"; // zinc-700
  }
}

function StatusIcon({ status, size = 12 }: { status: PipelineStageStatus; size?: number }) {
  switch (status) {
    case "ok":      return <CheckCircle2 size={size} className="text-emerald-400 shrink-0" />;
    case "fail":    return <XCircle size={size} className="text-rose-400 shrink-0" />;
    case "skip":    return <SkipForward size={size} className="text-amber-400 shrink-0" />;
    case "pending": return <Clock size={size} className="text-zinc-600 shrink-0" />;
  }
}

function verdictColor(verdict: string): string {
  if (verdict === "SUGGEST_REMEDIATION") return "text-amber-400";
  if (verdict === "HITL_PENDING") return "text-violet-400";
  if (verdict === "EXECUTE_MUTATE") return "text-rose-400";
  return "text-zinc-400";
}

function fmtElapsed(ms: number): string {
  if (ms === 0) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${ms}ms`;
}

function fmtAgo(epochSec: number): string {
  const delta = Math.floor(Date.now() / 1000 - epochSec);
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  return `${Math.floor(delta / 3600)}h ago`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Hooks
// ─────────────────────────────────────────────────────────────────────────────

// Real-time pipeline events over SSE (proxied through /api/trace/stream).
// Increments `eventSeq` on every stage event so data hooks can refetch on demand
// instead of waiting for the slow poll. `connected` drives the live/polling badge.
function useTraceEventStream() {
  const [connected, setConnected] = useState(false);
  const [eventSeq, setEventSeq] = useState(0);
  const [lastTraceId, setLastTraceId] = useState<string>("");

  useEffect(() => {
    const es = new EventSource("/api/trace/stream");
    es.onopen = () => setConnected(true);
    es.onmessage = (ev) => {
      setEventSeq((n) => n + 1);
      try {
        const d = JSON.parse(ev.data) as { trace_id?: string };
        if (d.trace_id) setLastTraceId(d.trace_id);
      } catch {
        /* keep-alive comment or malformed frame — ignore */
      }
    };
    es.onerror = () => setConnected(false);
    return () => es.close();
  }, []);

  return { connected, eventSeq, lastTraceId };
}

function useRecentTraces(liveSeq: number, connected: boolean) {
  const [traces, setTraces] = useState<RecentTrace[]>([]);
  const [source, setSource] = useState<string>("loading");

  const fetch_ = useCallback(() => {
    fetch("/api/trace/recent", { cache: "no-store" })
      .then((r) => r.json())
      .then((d: { traces: RecentTrace[]; source: string }) => {
        setTraces(d.traces ?? []);
        setSource(d.source ?? "ok");
      })
      .catch(() => setSource("error"));
  }, []);

  // Refetch immediately on each live event; poll as a fallback (slower when SSE up).
  useEffect(() => {
    fetch_();
    const t = setInterval(fetch_, connected ? FALLBACK_POLL_MS : POLL_INTERVAL_MS);
    return () => clearInterval(t);
  }, [fetch_, liveSeq, connected]);

  return { traces, source };
}

function usePipelineTrace(traceId: string | null, liveSeq: number, connected: boolean) {
  const [pipeline, setPipeline] = useState<PipelineResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const fetch_ = useCallback(() => {
    if (!traceId) return;
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    setLoading(true);
    fetch(`/api/trace/${encodeURIComponent(traceId)}/pipeline`, {
      cache: "no-store",
      signal: ctrl.signal,
    })
      .then((r) => r.json())
      .then((d: PipelineResponse) => {
        setPipeline(d);
        setLoading(false);
      })
      .catch((e) => {
        if (e.name !== "AbortError") setLoading(false);
      });
  }, [traceId]);

  // Refetch on selection change, on each live event, and via slow poll fallback.
  useEffect(() => {
    setPipeline(null);
    fetch_();
    const t = setInterval(fetch_, connected ? FALLBACK_POLL_MS : POLL_INTERVAL_MS);
    return () => {
      clearInterval(t);
      abortRef.current?.abort();
    };
  }, [fetch_, liveSeq, connected]);

  return { pipeline, loading };
}

// ─────────────────────────────────────────────────────────────────────────────
// Sub-components
// ─────────────────────────────────────────────────────────────────────────────

function LaneBadge({ lane }: { lane: string }) {
  return (
    <span className={`text-[9px] font-bold tracking-widest px-1.5 py-0.5 border rounded uppercase ${LANE_TEXT[lane] ?? "text-zinc-400"} border-current/30`}>
      {LANE_LABEL[lane] ?? lane}
    </span>
  );
}

function TraceListItem({
  trace,
  selected,
  onClick,
}: {
  trace: RecentTrace;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`
        w-full text-left px-3 py-2.5 border-l-2 transition-colors duration-150
        ${selected
          ? `${LANE_BORDER[trace.lane] ?? "border-l-zinc-600"} bg-zinc-800/60`
          : "border-l-transparent hover:bg-zinc-900/60 hover:border-l-zinc-700"
        }
      `}
    >
      <div className="flex items-center gap-1.5 mb-1">
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${LANE_DOT[trace.lane] ?? "bg-zinc-500"}`} />
        <LaneBadge lane={trace.lane} />
        <span className={`ml-auto text-[8px] tabular-nums ${verdictColor(trace.verdict)}`}>
          {trace.verdict}
        </span>
      </div>
      <div className="text-[10px] text-zinc-300 font-mono truncate">{trace.trace_id}</div>
      <div className="flex items-center justify-between mt-1">
        <span className="text-[8px] text-zinc-600 uppercase tracking-wider">{trace.current_stage}</span>
        <span className="text-[8px] text-zinc-700">{fmtAgo(trace.updated_at)}</span>
      </div>
    </button>
  );
}

function StageNode({
  entry,
  selected,
  onClick,
}: {
  entry: PipelineStageEntry;
  selected: boolean;
  onClick: () => void;
}) {
  const reduced = typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  return (
    <button
      onClick={onClick}
      aria-label={`Stage ${entry.stage} — ${entry.status}`}
      className={`
        relative flex flex-col items-center gap-1 px-2.5 py-2 rounded-lg border text-center cursor-pointer
        min-w-[72px] max-w-[80px]
        ${statusBg(entry.status)}
        ${statusRing(entry.status)}
        ${selected ? "ring-2 ring-offset-1 ring-offset-zinc-950 ring-amber-500" : ""}
        focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400
        ${reduced ? "" : "transition-transform duration-150 ease-out hover:scale-105 hover:-translate-y-0.5"}
        shadow
      `}
    >
      <StatusIcon status={entry.status} size={11} />
      <span className={`text-[8px] font-bold tracking-widest uppercase leading-none ${statusColor(entry.status)}`}>
        {entry.stage}
      </span>
      <span className="text-[7px] text-zinc-600 tabular-nums leading-none">
        {fmtElapsed(entry.elapsed_ms)}
      </span>
    </button>
  );
}

// SVG connector arrow between stage nodes
function Connector({ status }: { status: PipelineStageStatus }) {
  const color = connectorColor(status);
  return (
    <svg
      aria-hidden="true"
      width="24"
      height="18"
      viewBox="0 0 24 18"
      fill="none"
      className="shrink-0 self-center"
    >
      <line x1="0" y1="9" x2="18" y2="9" stroke={color} strokeWidth="1.5" />
      <polyline points="12,4 20,9 12,14" fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

function DetailPanel({
  stage,
  pipeline,
  onClose,
}: {
  stage: PipelineStage;
  pipeline: PipelineResponse;
  onClose: () => void;
}) {
  const entry = pipeline.stages.find((s) => s.stage === stage);
  if (!entry) return null;

  const isAnalysis = ["RAG", "LLM", "SCHEMA"].includes(stage);
  const sessionUrl = `/trace/${encodeURIComponent(pipeline.trace_id)}`;

  return (
    <div className="border-t border-zinc-800 bg-zinc-950/80 px-4 py-3 flex flex-col gap-2">
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <StatusIcon status={entry.status} size={13} />
          <span className={`text-[11px] font-bold uppercase tracking-widest ${statusColor(entry.status)}`}>
            {entry.stage}
          </span>
          <span className="text-[9px] text-zinc-600">{STAGE_GROUPS[stage]}</span>
        </div>
        <button
          onClick={onClose}
          className="text-zinc-600 hover:text-zinc-400 transition-colors text-[10px] px-1"
          aria-label="Close detail"
        >✕</button>
      </div>

      <p className="text-[9px] text-zinc-500 leading-relaxed">{STAGE_DESCRIPTIONS[stage]}</p>

      {entry.detail && (
        <div className="bg-zinc-900 rounded px-2.5 py-2 border border-zinc-800">
          <p className="text-[9px] text-zinc-400 font-mono break-all leading-relaxed">{entry.detail}</p>
        </div>
      )}

      <div className="flex items-center gap-4 text-[8px] text-zinc-700 tabular-nums">
        <span>ts: {new Date(entry.ts * 1000).toLocaleTimeString()}</span>
        {entry.elapsed_ms > 0 && <span>elapsed: {fmtElapsed(entry.elapsed_ms)}</span>}
      </div>

      {isAnalysis && (
        <a
          href={sessionUrl}
          className="inline-flex items-center gap-1 text-[9px] text-amber-400/80 hover:text-amber-300 transition-colors mt-0.5"
        >
          <span>View full diagnosis session</span>
          <ChevronRight size={9} />
        </a>
      )}
    </div>
  );
}

function GroupLabel({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-1 mb-1">
      <span className="text-[7px] uppercase tracking-widest text-zinc-700">{label}</span>
      <div className="flex-1 h-px bg-zinc-800/60" />
    </div>
  );
}

// Group stage nodes by logical group, with group headers
function FlowDiagram({
  stages,
  selectedStage,
  onSelectStage,
}: {
  stages: PipelineStageEntry[];
  selectedStage: PipelineStage | null;
  onSelectStage: (s: PipelineStage) => void;
}) {
  // Render as a single scrollable horizontal row with separators between groups
  const groups: { label: string; items: PipelineStageEntry[] }[] = [];
  let cur: { label: string; items: PipelineStageEntry[] } | null = null;

  for (const entry of stages) {
    const grp = STAGE_GROUPS[entry.stage] ?? "Other";
    if (!cur || cur.label !== grp) {
      cur = { label: grp, items: [] };
      groups.push(cur);
    }
    cur.items.push(entry);
  }

  return (
    <div className="overflow-x-auto pb-2">
      <div className="flex items-end gap-0 min-w-max">
        {groups.map((group, gi) => (
          <div key={group.label} className="flex items-end gap-0">
            {/* Group bracket */}
            <div className="flex flex-col">
              <GroupLabel label={group.label} />
              <div className="flex items-center gap-0">
                {group.items.map((entry, i) => {
                  const isLast = i === group.items.length - 1;
                  const globalIdx = stages.findIndex((s) => s.stage === entry.stage);
                  const nextEntry = globalIdx < stages.length - 1 ? stages[globalIdx + 1] : null;

                  return (
                    <div key={entry.stage} className="flex items-center">
                      <StageNode
                        entry={entry}
                        selected={selectedStage === entry.stage}
                        onClick={() => onSelectStage(entry.stage as PipelineStage)}
                      />
                      {/* connector to next stage */}
                      {(nextEntry || !isLast) && (
                        <Connector status={nextEntry?.status ?? entry.status} />
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
            {/* Group separator — vertical line between groups */}
            {gi < groups.length - 1 && (
              <div className="self-stretch mx-2 w-px bg-zinc-800 opacity-50" />
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function BannerStrip({ pipeline }: { pipeline: PipelineResponse }) {
  const killswitchEntry = pipeline.stages.find((s) => s.stage === "KILLSWITCH");
  const cratEntry = pipeline.stages.find((s) => s.stage === "CRAT");
  const totalOk = pipeline.stages.filter((s) => s.status === "ok").length;
  const totalFail = pipeline.stages.filter((s) => s.status === "fail").length;

  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1 px-4 py-2 border-b border-zinc-800 bg-zinc-950/60 text-[9px] font-mono">
      {/* Lane + verdict */}
      <div className="flex items-center gap-2">
        <LaneBadge lane={pipeline.lane} />
        <span className={`font-semibold ${verdictColor(pipeline.verdict)}`}>{pipeline.verdict}</span>
      </div>

      {/* Kill-switch */}
      <div className="flex items-center gap-1 text-zinc-500">
        <Shield size={9} className={killswitchEntry?.status === "ok" ? "text-emerald-500" : "text-zinc-600"} />
        <span>kill-switch:</span>
        <span className={killswitchEntry?.status === "ok" ? "text-emerald-400" : "text-zinc-600"}>
          {killswitchEntry ? (killswitchEntry.detail.includes("false") ? "engaged" : "open") : "—"}
        </span>
      </div>

      {/* CRAT */}
      <div className="flex items-center gap-1 text-zinc-500">
        <CheckCircle2 size={9} className={cratEntry?.status === "ok" ? "text-sky-500" : "text-zinc-600"} />
        <span>crat:</span>
        <span className={cratEntry?.status === "ok" ? "text-sky-400" : "text-zinc-600"}>
          {cratEntry?.status === "ok"
            ? (cratEntry.detail.match(/block#(\d+)/) ?? ["", "—"])[1]
            : "—"}
        </span>
      </div>

      {/* Stage summary */}
      <div className="flex items-center gap-2 ml-auto text-zinc-600">
        <span className="text-emerald-500">{totalOk} ok</span>
        {totalFail > 0 && <span className="text-rose-500">{totalFail} fail</span>}
        <span>| started {fmtAgo(pipeline.started_at)}</span>
        {pipeline.source === "mock" && (
          <span className="text-[7px] border border-zinc-700 px-1 rounded text-zinc-600">mock</span>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────────────

export default function PipelinePage() {
  const [now, setNow] = useState(() => Date.now());
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);
  const [selectedStage, setSelectedStage] = useState<PipelineStage | null>(null);

  const { connected, eventSeq, lastTraceId } = useTraceEventStream();
  const { traces, source: listSource } = useRecentTraces(eventSeq, connected);
  // Bump pipeline refetch only when the live event concerns the selected trace
  // (or on initial selection); avoids refetching on every unrelated event.
  const pipelineSeq = lastTraceId === selectedTraceId ? eventSeq : 0;
  const { pipeline, loading } = usePipelineTrace(selectedTraceId, pipelineSeq, connected);

  // Auto-select first trace
  useEffect(() => {
    if (!selectedTraceId && traces.length > 0) {
      setSelectedTraceId(traces[0].trace_id);
    }
  }, [traces, selectedTraceId]);

  // Clock
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(t);
  }, []);

  const handleSelectTrace = (id: string) => {
    setSelectedTraceId(id);
    setSelectedStage(null);
  };

  const handleSelectStage = (stage: PipelineStage) => {
    setSelectedStage((prev) => (prev === stage ? null : stage));
  };

  return (
    <div className="flex h-full">
      <Sidebar />
      <main className="flex-1 flex flex-col overflow-hidden bg-zinc-950 font-mono text-[11px]">

        {/* ── Top navigation bar ── */}
        <header className="sticky top-0 z-10 flex items-center justify-between px-4 h-9 border-b border-zinc-800 bg-zinc-950 shrink-0">
          <div className="flex items-center gap-3 text-[10px]">
            <span className="text-amber-400 font-semibold tracking-widest uppercase">Pipeline</span>
            <span className="text-zinc-700">live incident flow</span>
            {listSource === "mock" && (
              <span className="text-[8px] border border-zinc-700 text-zinc-600 px-1 rounded">mock data</span>
            )}
          </div>
          <div className="flex items-center gap-3 text-[10px]">
            <span className="text-zinc-600">{new Date(now).toLocaleTimeString()}</span>
            <span className={`flex items-center gap-1 ${connected ? "text-emerald-400" : "text-zinc-500"}`}>
              <Radio size={9} className={connected ? "animate-pulse" : ""} />
              {connected ? "live (SSE)" : `polling ${POLL_INTERVAL_MS / 1000}s`}
            </span>
          </div>
        </header>

        {/* ── Body: left list + center flow ── */}
        <div className="flex flex-1 overflow-hidden">

          {/* LEFT: Incident list */}
          <aside className="w-56 shrink-0 flex flex-col border-r border-zinc-800 overflow-y-auto">
            <div className="px-3 py-2 border-b border-zinc-800/60">
              <span className="text-[8px] uppercase tracking-widest text-zinc-600">Active Traces</span>
            </div>
            {traces.length === 0 ? (
              <div className="flex flex-col items-center justify-center flex-1 gap-2 text-zinc-700 px-4 text-center">
                <AlertTriangle size={18} className="opacity-40" />
                <p className="text-[9px]">No active traces</p>
              </div>
            ) : (
              <div className="flex flex-col divide-y divide-zinc-800/50">
                {traces.map((t) => (
                  <TraceListItem
                    key={t.trace_id}
                    trace={t}
                    selected={t.trace_id === selectedTraceId}
                    onClick={() => handleSelectTrace(t.trace_id)}
                  />
                ))}
              </div>
            )}
          </aside>

          {/* CENTER: Flow diagram + detail */}
          <section className="flex-1 flex flex-col overflow-hidden" aria-label="Pipeline flow">
            {!selectedTraceId ? (
              <div className="flex items-center justify-center flex-1 text-zinc-700">
                <p className="text-[10px]">Select a trace to inspect</p>
              </div>
            ) : loading && !pipeline ? (
              <div className="flex items-center justify-center flex-1 text-zinc-700 gap-2">
                <Radio size={12} className="animate-pulse text-amber-500" />
                <p className="text-[10px]">Loading pipeline…</p>
              </div>
            ) : pipeline && pipeline.found === false ? (
              <div className="flex items-center justify-center flex-1 text-zinc-600">
                <p className="text-[10px]">No pipeline data for this trace</p>
              </div>
            ) : pipeline ? (
              <>
                {/* Trace header */}
                <div className="px-4 py-2 border-b border-zinc-800 shrink-0">
                  <div className="flex items-center gap-2">
                    <span className="text-[9px] text-zinc-500">trace</span>
                    <span className="text-[10px] text-zinc-200">{pipeline.trace_id}</span>
                  </div>
                </div>

                {/* Banner strip: kill-switch, CRAT, verdict */}
                <BannerStrip pipeline={pipeline} />

                {/* Flow canvas */}
                <div className="flex-1 flex flex-col overflow-auto px-4 py-4 gap-4">
                  <div>
                    <SectionLabel text="Pipeline stages — click a node to inspect" />
                    <FlowDiagram
                      stages={pipeline.stages}
                      selectedStage={selectedStage}
                      onSelectStage={handleSelectStage}
                    />
                  </div>

                  {/* Legend */}
                  <div className="flex items-center gap-4 text-[8px] text-zinc-600">
                    {(["ok", "fail", "skip", "pending"] as PipelineStageStatus[]).map((s) => (
                      <span key={s} className={`flex items-center gap-1 ${statusColor(s)}`}>
                        <StatusIcon status={s} size={9} />
                        {s}
                      </span>
                    ))}
                  </div>
                </div>

                {/* Stage detail drawer */}
                {selectedStage && (
                  <DetailPanel
                    stage={selectedStage}
                    pipeline={pipeline}
                    onClose={() => setSelectedStage(null)}
                  />
                )}
              </>
            ) : null}
          </section>
        </div>
      </main>
    </div>
  );
}
