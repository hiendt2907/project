"use client";

import { useEffect, useState, useCallback } from "react";
import { Sidebar } from "@/components/sidebar";
import { Radio, AlertTriangle, Trash2 } from "lucide-react";
import { LANE_BORDER, LANE_DOT } from "@/components/shared/lane-tokens";
import type { RecentTrace } from "@/app/api/trace/recent/route";
import {
  useTraceEventStream,
  usePipelineTrace,
  TracePipelineView,
  LaneBadge,
  verdictColor,
  fmtAgo,
  POLL_INTERVAL_MS,
  FALLBACK_POLL_MS,
} from "@/components/pipeline/trace-view";

// ─────────────────────────────────────────────────────────────────────────────
// Recent-traces list hook (page-specific — the shared module owns single-trace view)
// ─────────────────────────────────────────────────────────────────────────────

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

  useEffect(() => {
    fetch_();
    const t = setInterval(fetch_, connected ? FALLBACK_POLL_MS : POLL_INTERVAL_MS);
    return () => clearInterval(t);
  }, [fetch_, liveSeq, connected]);

  return { traces, source, refresh: fetch_ };
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

// ─────────────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────────────

function usePurgeTraces(onPurged: () => void) {
  const [purging, setPurging] = useState(false);

  const purge = useCallback(async () => {
    if (purging) return;
    if (!window.confirm("Purge all active traces? This clears the pipeline dashboard view.")) {
      return;
    }
    setPurging(true);
    try {
      await fetch("/api/trace/purge", { method: "POST" });
      onPurged();
    } catch {
      // best-effort — the list will simply refresh on the next poll/SSE tick
    } finally {
      setPurging(false);
    }
  }, [purging, onPurged]);

  return { purge, purging };
}

export default function PipelinePage() {
  const [now, setNow] = useState(() => Date.now());
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);

  const { connected, eventSeq, lastTraceId } = useTraceEventStream();
  const { traces, source: listSource, refresh: refreshTraces } = useRecentTraces(eventSeq, connected);
  const { purge, purging } = usePurgeTraces(() => {
    setSelectedTraceId(null);
    refreshTraces();
  });
  const pipelineSeq = lastTraceId === selectedTraceId ? eventSeq : 0;
  const { pipeline, loading } = usePipelineTrace(selectedTraceId, pipelineSeq, connected);

  useEffect(() => {
    if (!selectedTraceId && traces.length > 0) {
      setSelectedTraceId(traces[0].trace_id);
    }
  }, [traces, selectedTraceId]);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="flex h-full">
      <Sidebar />
      <main className="flex-1 flex flex-col overflow-hidden bg-zinc-950 font-mono text-[11px]">

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

        <div className="flex flex-1 overflow-hidden">

          {/* LEFT: Incident list */}
          <aside className="w-56 shrink-0 flex flex-col border-r border-zinc-800 overflow-y-auto">
            <div className="px-3 py-2 border-b border-zinc-800/60 flex items-center justify-between">
              <span className="text-[8px] uppercase tracking-widest text-zinc-600">Active Traces</span>
              <button
                onClick={purge}
                disabled={purging || traces.length === 0}
                title="Purge all active traces"
                className="flex items-center gap-1 text-[8px] uppercase tracking-wider text-zinc-600 hover:text-red-400 disabled:opacity-30 disabled:hover:text-zinc-600 transition-colors"
              >
                <Trash2 size={10} />
                {purging ? "purging…" : "purge"}
              </button>
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
                    onClick={() => setSelectedTraceId(t.trace_id)}
                  />
                ))}
              </div>
            )}
          </aside>

          {/* CENTER: Flow diagram + detail */}
          <section className="flex-1 flex flex-col overflow-auto" aria-label="Pipeline flow">
            {!selectedTraceId ? (
              <div className="flex items-center justify-center flex-1 text-zinc-700">
                <p className="text-[10px]">Select a trace to inspect</p>
              </div>
            ) : (
              <TracePipelineView pipeline={pipeline} loading={loading} />
            )}
          </section>
        </div>
      </main>
    </div>
  );
}
