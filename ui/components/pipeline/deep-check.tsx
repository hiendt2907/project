"use client";

// Inline deep-check report for a trace: multi-turn diagnosis session (turns,
// reasoning, command results) when present, else the single-pass advisory
// (verification steps, impact chain, remediation, forecast). Used inside the
// Simulator so the deep checks are visible on the same screen, not only behind a link.

import { useEffect, useState } from "react";
import Link from "next/link";
import { ListChecks, Wrench, TrendingUp, GitBranch, FileSearch, ChevronRight, Terminal, BrainCircuit, ScrollText } from "lucide-react";
import type { TraceSession } from "@/app/api/trace/[id]/session/route";
import type { TraceAdvisory } from "@/app/api/trace/[id]/advisory/route";
import type { TraceBrain } from "@/app/api/trace/[id]/brain/route";
import type { TraceLogs, TraceLogEntry } from "@/app/api/trace/[id]/logs/route";

const PHASE_LOG_COLOR: Record<string, string> = {
  error: "text-rose-400",
  warn: "text-amber-400",
  info: "text-zinc-400",
};

function LogsSection({ logs }: { logs: TraceLogEntry[] }) {
  if (logs.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5 border border-zinc-800 rounded-lg bg-zinc-950 p-2.5">
      <div className="flex items-center gap-1.5">
        <ScrollText size={11} className="text-zinc-500" />
        <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-400">Raw logs per phase ({logs.length})</span>
      </div>
      <div className="flex flex-col gap-0.5 max-h-56 overflow-y-auto font-mono">
        {logs.map((l, i) => (
          <div key={i} className="flex items-start gap-2 text-[8px] leading-relaxed">
            <span className="text-zinc-700 tabular-nums shrink-0">{new Date(l.ts * 1000).toLocaleTimeString()}</span>
            <span className="text-cyan-500/70 uppercase shrink-0 w-16">{l.phase}</span>
            <span className={`${PHASE_LOG_COLOR[l.level] ?? "text-zinc-400"} break-all`}>{l.line}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function BrainSection({ brain }: { brain: TraceBrain }) {
  if (!brain.found || !brain.turns?.length) return null;
  return (
    <div className="flex flex-col gap-2 border border-cyan-900/50 bg-cyan-950/10 rounded-lg p-2.5">
      <div className="flex items-center gap-1.5">
        <BrainCircuit size={12} className="text-cyan-400" />
        <span className="text-[9px] font-bold uppercase tracking-widest text-cyan-300">Redis 2nd brain</span>
        <span className="text-[8px] text-zinc-500">{brain.turn_count} turns · top {(brain.top_score ?? 0).toFixed(3)}</span>
        {brain.confident && <span className="text-[8px] text-emerald-400 font-bold">CONFIDENT</span>}
      </div>
      {brain.turns.map((t) => (
        <div key={t.turn} className="bg-zinc-950 rounded border border-zinc-800 px-2 py-1.5">
          <div className="flex items-center gap-2 mb-0.5">
            <span className="text-[8px] font-bold text-cyan-400 uppercase">Turn {t.turn}</span>
            <span className="text-[8px] text-zinc-600">top {t.top_score.toFixed(3)}</span>
          </div>
          {t.hits.slice(0, 3).map((h, i) => (
            <div key={i} className="text-[8px] text-zinc-400 leading-relaxed">
              <span className="text-zinc-600">[{h.collection} {h.score.toFixed(2)}]</span> {h.summary.slice(0, 140)}
            </div>
          ))}
          {t.hits.length === 0 && <span className="text-[8px] text-zinc-700">no new knowledge</span>}
        </div>
      ))}
    </div>
  );
}

export function DeepCheckPanel({ traceId, liveSeq }: { traceId: string; liveSeq: number }) {
  const [session, setSession] = useState<TraceSession | null>(null);
  const [advisory, setAdvisory] = useState<TraceAdvisory | null>(null);
  const [brain, setBrain] = useState<TraceBrain | null>(null);
  const [logs, setLogs] = useState<TraceLogEntry[]>([]);

  useEffect(() => {
    let alive = true;
    Promise.all([
      fetch(`/api/trace/${encodeURIComponent(traceId)}/session`, { cache: "no-store" }).then((r) => r.json()).catch(() => null),
      fetch(`/api/trace/${encodeURIComponent(traceId)}/advisory`, { cache: "no-store" }).then((r) => r.json()).catch(() => null),
      fetch(`/api/trace/${encodeURIComponent(traceId)}/brain`, { cache: "no-store" }).then((r) => r.json()).catch(() => null),
      fetch(`/api/trace/${encodeURIComponent(traceId)}/logs`, { cache: "no-store" }).then((r) => r.json()).catch(() => null),
    ]).then(([s, a, b, l]) => {
      if (!alive) return;
      setSession(s);
      setAdvisory(a);
      setBrain(b);
      setLogs((l as TraceLogs)?.logs ?? []);
    });
    return () => { alive = false; };
  }, [traceId, liveSeq]);

  const hasSession = session?.found && session.turns.length > 0;
  const adv = advisory?.found ? advisory.advisory : undefined;
  const hasBrain = brain?.found && (brain.turns?.length ?? 0) > 0;
  if (!hasSession && !adv && !hasBrain && logs.length === 0) {
    return (
      <div className="px-4 py-3 border-t border-zinc-800 text-[9px] text-zinc-600">
        Deep diagnosis report appears here once the analyst finishes (multi-turn loop for
        critical/high urgency, else single-pass advisory).
      </div>
    );
  }

  return (
    <div className="border-t border-zinc-800 px-4 py-3 flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <FileSearch size={12} className="text-amber-400" />
        <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-300">Deep check report</span>
        <Link href={`/trace/${encodeURIComponent(traceId)}`} className="ml-auto flex items-center gap-0.5 text-[9px] text-amber-400/80 hover:text-amber-300">
          full view <ChevronRight size={10} />
        </Link>
      </div>

      {hasBrain && brain && <BrainSection brain={brain} />}

      {hasSession && session && (
        <div className="flex flex-col gap-2">
          <span className="text-[8px] uppercase tracking-widest text-zinc-600">
            Diagnosis loop — {session.total_turns} turns · confidence {(session.final.confidence * 100).toFixed(0)}%
          </span>
          {session.final.root_cause && <p className="text-[10px] text-zinc-200 leading-relaxed">{session.final.root_cause}</p>}
          {session.turns.map((t) => (
            <div key={t.turn} className="bg-zinc-950 rounded border border-zinc-800 px-2.5 py-2">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-[8px] font-bold text-amber-400 uppercase">Turn {t.turn}</span>
                <span className="text-[8px] text-zinc-600">conf {(t.confidence * 100).toFixed(0)}%</span>
              </div>
              {t.hypothesis && <p className="text-[9px] text-zinc-400 leading-relaxed">{t.hypothesis}</p>}
              {t.command_results.length > 0 && (
                <div className="mt-1 flex flex-col gap-1">
                  {t.command_results.map((c) => (
                    <div key={c.cmd_id} className="flex items-center gap-1.5 text-[8px]">
                      <Terminal size={9} className="text-zinc-600 shrink-0" />
                      <span className={c.blocked ? "text-rose-400" : c.rc === 0 ? "text-emerald-400" : "text-amber-400"}>
                        {c.blocked ? "BLOCKED" : `rc=${c.rc}`}
                      </span>
                      <span className="text-zinc-400 font-mono truncate">{c.command_str}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {!hasSession && adv && (
        <div className="flex flex-col gap-2">
          <span className="text-[8px] uppercase tracking-widest text-zinc-600">Advisory — {adv.verdict} · {adv.confidence}</span>
          {adv.root_cause && <p className="text-[10px] text-zinc-200 leading-relaxed">{adv.root_cause}</p>}

          {adv.verification_steps?.length > 0 && (
            <div className="flex flex-col gap-1">
              <span className="flex items-center gap-1 text-[8px] uppercase tracking-widest text-zinc-600"><ListChecks size={10} /> Verification</span>
              {adv.verification_steps.map((s) => (
                <div key={s.order} className="text-[8px] text-zinc-400">
                  <span className="text-zinc-600">{s.layer}</span> <span className="font-mono text-zinc-300">{s.command}</span> — {s.rationale}
                </div>
              ))}
            </div>
          )}

          {adv.impact_chain && adv.impact_chain.length > 0 && (
            <div className="flex flex-col gap-0.5">
              <span className="flex items-center gap-1 text-[8px] uppercase tracking-widest text-zinc-600"><GitBranch size={10} /> Impact chain</span>
              {adv.impact_chain.map((c, i) => (
                <p key={i} className="text-[8px] text-zinc-400">
                  <span className="text-rose-400">{c.cause}</span> → <span className="text-amber-400">{c.mechanism}</span> → <span className="text-zinc-300">{c.effect}</span>
                </p>
              ))}
            </div>
          )}

          {adv.proposed_remediation?.length > 0 && (
            <div className="flex flex-col gap-0.5">
              <span className="flex items-center gap-1 text-[8px] uppercase tracking-widest text-zinc-600"><Wrench size={10} /> Remediation</span>
              <ol className="list-decimal list-inside text-[8px] text-zinc-400">
                {adv.proposed_remediation.map((r) => <li key={r.order}>{r.action}</li>)}
              </ol>
            </div>
          )}

          {adv.forecast?.forecasts && adv.forecast.forecasts.length > 0 && (
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="flex items-center gap-1 text-[8px] uppercase tracking-widest text-zinc-600"><TrendingUp size={10} /> Forecast</span>
              {adv.forecast.forecasts.map((f) => (
                <span key={f.timeframe} className="text-[8px] text-zinc-500">{f.timeframe}:<span className="text-amber-400">{f.severity}</span></span>
              ))}
            </div>
          )}
        </div>
      )}

      <LogsSection logs={logs} />
    </div>
  );
}
