"use client";

// TraceSessionView — T3 drill-down. Fetches the multi-turn diagnosis session
// for a trace and renders per-turn LLM reasoning + agent commands (metadata-only
// previews). Shared by the admin drawer and the operator detail panel.

import { useEffect, useState } from "react";
import type { TraceSession, DiagnosisTurn, CommandResult } from "@/app/api/trace/[id]/session/route";
import { LANE_TEXT, LANE_LABEL } from "@/components/shared/lane-tokens";

function confidenceColor(c: number): string {
  if (c >= 0.8) return "text-emerald-400";
  if (c >= 0.5) return "text-amber-400";
  return "text-rose-400";
}

function CommandResultRow({ res }: { res: CommandResult }) {
  const [open, setOpen] = useState(false);
  const rcColor = res.blocked
    ? "text-rose-400"
    : res.rc === 0
    ? "text-emerald-400"
    : "text-amber-400";
  return (
    <div className="border border-zinc-800/60 rounded mb-1 bg-zinc-900/30">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full text-left px-2 py-1 flex items-center gap-2 hover:bg-zinc-900/60 transition-colors"
      >
        <span className="text-emerald-300 text-[9px] shrink-0">$</span>
        <code className="text-[9px] text-emerald-300 font-mono flex-1 truncate">{res.command_str || "(no command)"}</code>
        {res.blocked ? (
          <span className="text-[8px] text-rose-400 border border-rose-500/30 px-1 rounded shrink-0">BLOCKED</span>
        ) : (
          <span className={`text-[8px] tabular-nums shrink-0 ${rcColor}`}>rc={res.rc}</span>
        )}
        <span className={`text-[8px] text-zinc-600 transition-transform shrink-0 ${open ? "rotate-180" : ""}`}>▾</span>
      </button>
      {open && (
        <div className="px-2 pb-1.5 pt-0.5 border-t border-zinc-800/60">
          {res.purpose && <p className="text-[8px] text-zinc-500 mb-1">purpose: {res.purpose}</p>}
          {res.blocked ? (
            <p className="text-[9px] text-rose-400/80">⛔ {res.block_reason}</p>
          ) : res.preview.total_lines === 0 ? (
            <p className="text-[8px] text-zinc-600">no output</p>
          ) : (
            <div className="font-mono text-[8px] text-zinc-400 bg-zinc-950/60 rounded px-1.5 py-1 overflow-x-auto">
              {res.preview.head.map((l, i) => (
                <div key={`h${i}`} className="whitespace-pre truncate">{l}</div>
              ))}
              {res.preview.truncated && (
                <div className="text-zinc-700 py-0.5">
                  … +{res.preview.total_lines - res.preview.head.length - res.preview.tail.length} dòng (metadata-only preview) …
                </div>
              )}
              {res.preview.tail.map((l, i) => (
                <div key={`t${i}`} className="whitespace-pre truncate">{l}</div>
              ))}
            </div>
          )}
          {res.stderr_preview && (
            <p className="text-[8px] text-amber-400/70 mt-1 truncate">stderr: {res.stderr_preview}</p>
          )}
        </div>
      )}
    </div>
  );
}

function TurnBlock({ turn }: { turn: DiagnosisTurn }) {
  return (
    <div className="relative pl-4 pb-3">
      <span className="absolute left-0 top-1 w-1.5 h-1.5 rounded-full bg-amber-400" />
      <span className="absolute left-[2.5px] top-3 bottom-0 w-px bg-zinc-800" />
      <div className="flex items-center gap-2 mb-1">
        <span className="text-[9px] text-amber-400 font-semibold uppercase tracking-wider">Turn {turn.turn}</span>
        <span className={`text-[9px] tabular-nums ${confidenceColor(turn.confidence)}`}>
          conf {(turn.confidence * 100).toFixed(0)}%
        </span>
        {turn.diagnosis_complete_claimed && (
          <span className="text-[8px] text-emerald-400 border border-emerald-500/30 px-1 rounded">complete</span>
        )}
      </div>
      {turn.reasoning && <p className="text-[9px] text-zinc-400 leading-relaxed mb-1">{turn.reasoning}</p>}
      {turn.hypothesis && (
        <p className="text-[9px] text-zinc-300 mb-1">
          <span className="text-zinc-600">hypothesis: </span>{turn.hypothesis}
        </p>
      )}
      {turn.evidence_gaps.length > 0 && (
        <p className="text-[8px] text-zinc-600 mb-1">gaps: {turn.evidence_gaps.join(" · ")}</p>
      )}
      {turn.command_results.length > 0 && (
        <div className="mt-1.5">
          {turn.command_results.map((r) => (
            <CommandResultRow key={r.cmd_id} res={r} />
          ))}
        </div>
      )}
    </div>
  );
}

export function TraceSessionView({ traceId }: { traceId: string }) {
  const [session, setSession] = useState<TraceSession | null>(null);
  const [error, setError] = useState(false);
  const [prevTrace, setPrevTrace] = useState(traceId);

  // Reset view when the trace changes (adjust-state-during-render pattern) so the
  // effect below does not have to call setState synchronously.
  if (traceId !== prevTrace) {
    setPrevTrace(traceId);
    setSession(null);
    setError(false);
  }

  useEffect(() => {
    let active = true;
    fetch(`/api/trace/${encodeURIComponent(traceId)}/session`)
      .then((r) => r.json())
      .then((d: TraceSession) => {
        if (active) setSession(d);
      })
      .catch(() => active && setError(true));
    return () => {
      active = false;
    };
  }, [traceId]);

  if (error) return <p className="text-[10px] text-rose-400">failed to load session</p>;
  if (!session) return <p className="text-[10px] text-zinc-600 animate-pulse">loading session…</p>;
  if (session.source === "error") return <p className="text-[10px] text-rose-400/80">✕ gateway unreachable — session unavailable</p>;
  if (!session.found) return <p className="text-[10px] text-zinc-600">no diagnosis session stored for this trace</p>;

  return (
    <div className="text-[10px] font-mono">
      {/* meta */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[9px] text-zinc-600 mb-3">
        <span className={LANE_TEXT[session.lane] ?? "text-zinc-500"}>{LANE_LABEL[session.lane] ?? session.lane}</span>
        <span>·</span>
        <span>agent:{session.agent_id || "—"}</span>
        <span>·</span>
        <span>probe:{session.probe || "—"}</span>
        <span>·</span>
        <span>{session.total_turns} turns</span>
        {session.degraded && (
          <span className="text-[8px] text-amber-400 border border-amber-500/30 px-1 rounded">degraded</span>
        )}
      </div>
      {session.degraded_reason && (
        <p className="text-[8px] text-amber-400/70 mb-2">{session.degraded_reason}</p>
      )}

      {/* turns timeline */}
      <div className="mb-3">
        {session.turns.map((t) => (
          <TurnBlock key={t.turn} turn={t} />
        ))}
      </div>

      {/* final */}
      <div className="border-t border-zinc-800 pt-2 space-y-2">
        <div>
          <p className="text-[8px] text-zinc-600 uppercase tracking-wider">root cause</p>
          <p className="text-zinc-300 leading-relaxed">{session.final.root_cause}</p>
        </div>
        {session.final.blast_radius && (
          <div>
            <p className="text-[8px] text-zinc-600 uppercase tracking-wider">🌐 blast radius</p>
            <p className="text-zinc-400 leading-relaxed">{session.final.blast_radius}</p>
          </div>
        )}
        {session.final.affected_components.length > 0 && (
          <div>
            <p className="text-[8px] text-zinc-600 uppercase tracking-wider">affected</p>
            <p className="text-zinc-400">{session.final.affected_components.join(" · ")}</p>
          </div>
        )}
        {session.final.remediation_steps.length > 0 && (
          <div>
            <p className="text-[8px] text-zinc-600 uppercase tracking-wider">remediation</p>
            <ol className="mt-0.5 space-y-0.5">
              {session.final.remediation_steps.map((s, i) => (
                <li key={i} className="flex gap-2">
                  <span className="text-zinc-700 shrink-0">{i + 1}.</span>
                  <code className="text-emerald-300 text-[9px] break-all">{s}</code>
                </li>
              ))}
            </ol>
          </div>
        )}
      </div>
    </div>
  );
}
