"use client";

// Diagnosis session view for a single trace. Reached from the pipeline / simulator
// "View full diagnosis session" link. Multi-turn sessions only exist for traces that
// went through the autonomous diagnosis loop (critical/high urgency). Single-pass
// advisory traces have no stored session — this renders an honest empty state rather
// than a 404, with a link back to the pipeline view which always has stage data.

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { Sidebar } from "@/components/sidebar";
import { ChevronLeft, Workflow, FileSearch, CheckCircle2, AlertCircle, ListChecks, Wrench, TrendingUp, GitBranch } from "lucide-react";
import type { TraceSession, DiagnosisTurn } from "@/app/api/trace/[id]/session/route";
import type { TraceAdvisory } from "@/app/api/trace/[id]/advisory/route";

function TurnCard({ turn }: { turn: DiagnosisTurn }) {
  const [open, setOpen] = useState(turn.turn === 1);
  return (
    <div className="border border-zinc-800 rounded-lg bg-zinc-900/40 overflow-hidden">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-zinc-900 transition-colors text-left"
      >
        <span className="text-[9px] font-bold uppercase tracking-widest text-amber-400">Turn {turn.turn}</span>
        <span className="text-[9px] text-zinc-600">confidence {(turn.confidence * 100).toFixed(0)}%</span>
        {turn.diagnosis_complete_claimed && (
          <span className="flex items-center gap-1 text-[8px] text-emerald-400">
            <CheckCircle2 size={9} /> complete
          </span>
        )}
        <span className="ml-auto text-zinc-600 text-[10px]">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="px-3 py-2 flex flex-col gap-2 border-t border-zinc-800/60">
          {turn.hypothesis && (
            <div>
              <span className="text-[8px] uppercase tracking-widest text-zinc-600">Hypothesis</span>
              <p className="text-[10px] text-zinc-300 leading-relaxed">{turn.hypothesis}</p>
            </div>
          )}
          {turn.reasoning && (
            <div>
              <span className="text-[8px] uppercase tracking-widest text-zinc-600">Reasoning</span>
              <p className="text-[10px] text-zinc-400 leading-relaxed whitespace-pre-wrap">{turn.reasoning}</p>
            </div>
          )}
          {turn.command_results.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <span className="text-[8px] uppercase tracking-widest text-zinc-600">Commands ({turn.command_results.length})</span>
              {turn.command_results.map((c) => (
                <div key={c.cmd_id} className="bg-zinc-950 rounded border border-zinc-800 px-2 py-1.5">
                  <div className="flex items-center gap-2">
                    <span className={`text-[8px] ${c.blocked ? "text-rose-400" : c.rc === 0 ? "text-emerald-400" : "text-amber-400"}`}>
                      {c.blocked ? "BLOCKED" : `rc=${c.rc}`}
                    </span>
                    <span className="text-[9px] text-zinc-300 font-mono truncate">{c.command_str}</span>
                  </div>
                  {c.purpose && <p className="text-[8px] text-zinc-600 mt-0.5">{c.purpose}</p>}
                  {c.preview.head.length > 0 && (
                    <pre className="text-[8px] text-zinc-500 mt-1 overflow-x-auto whitespace-pre-wrap break-all">
                      {c.preview.head.join("\n")}
                      {c.preview.truncated ? `\n… (${c.preview.total_lines} lines)` : ""}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function AdvisoryReport({ adv }: { adv: NonNullable<TraceAdvisory["advisory"]> }) {
  return (
    <div className="flex flex-col gap-4 max-w-3xl">
      <div className="border border-zinc-800 rounded-lg bg-zinc-900/40 p-3 flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <FileSearch size={13} className="text-amber-400" />
          <span className="text-[10px] font-bold uppercase tracking-widest text-zinc-200">Advisory (single-pass)</span>
          <span className="ml-auto text-[8px] text-zinc-600">{adv.verdict} · confidence {adv.confidence}</span>
        </div>
        {adv.root_cause && <p className="text-[11px] text-zinc-200 leading-relaxed">{adv.root_cause}</p>}
        {adv.affected_workload && (
          <p className="text-[9px] text-zinc-500"><span className="text-zinc-600 uppercase tracking-wider">Affected:</span> {adv.affected_workload}</p>
        )}
      </div>

      {adv.verification_steps?.length > 0 && (
        <div className="border border-zinc-800 rounded-lg bg-zinc-900/40 p-3 flex flex-col gap-2">
          <span className="flex items-center gap-1.5 text-[9px] uppercase tracking-widest text-zinc-500"><ListChecks size={11} /> Verification steps</span>
          {adv.verification_steps.map((s) => (
            <div key={s.order} className="bg-zinc-950 rounded border border-zinc-800 px-2 py-1.5">
              <div className="flex items-center gap-2">
                <span className="text-[8px] text-amber-400">#{s.order}</span>
                <span className="text-[8px] text-zinc-600 uppercase">{s.layer}</span>
                <span className="text-[9px] text-zinc-300 font-mono truncate">{s.command}</span>
              </div>
              <p className="text-[8px] text-zinc-500 mt-0.5">{s.rationale} → expect: {s.expected_output}</p>
            </div>
          ))}
        </div>
      )}

      {adv.impact_chain && adv.impact_chain.length > 0 && (
        <div className="border border-zinc-800 rounded-lg bg-zinc-900/40 p-3 flex flex-col gap-2">
          <span className="flex items-center gap-1.5 text-[9px] uppercase tracking-widest text-zinc-500"><GitBranch size={11} /> Impact chain</span>
          {adv.impact_chain.map((c, i) => (
            <div key={i} className="text-[9px] text-zinc-400 leading-relaxed">
              <span className="text-rose-400">{c.cause}</span> → <span className="text-amber-400">{c.mechanism}</span> → <span className="text-zinc-300">{c.effect}</span>
              <span className="text-[8px] text-zinc-600"> [{c.evidence_lane} · {c.confidence}]</span>
            </div>
          ))}
        </div>
      )}

      {adv.proposed_remediation?.length > 0 && (
        <div className="border border-zinc-800 rounded-lg bg-zinc-900/40 p-3 flex flex-col gap-2">
          <span className="flex items-center gap-1.5 text-[9px] uppercase tracking-widest text-zinc-500"><Wrench size={11} /> Proposed remediation</span>
          <ol className="list-decimal list-inside text-[10px] text-zinc-400 leading-relaxed">
            {adv.proposed_remediation.map((r) => (
              <li key={r.order}>{r.action} {r.approval_required && <span className="text-[8px] text-amber-400">(approval required)</span>}</li>
            ))}
          </ol>
        </div>
      )}

      {adv.forecast?.forecasts && adv.forecast.forecasts.length > 0 && (
        <div className="border border-zinc-800 rounded-lg bg-zinc-900/40 p-3 flex flex-col gap-2">
          <span className="flex items-center gap-1.5 text-[9px] uppercase tracking-widest text-zinc-500"><TrendingUp size={11} /> Forecast ({adv.forecast.method})</span>
          <div className="flex flex-wrap gap-1.5">
            {adv.forecast.forecasts.map((f) => (
              <span key={f.timeframe} className="text-[8px] border border-zinc-800 rounded px-1.5 py-0.5 text-zinc-400">
                {f.timeframe}: <span className="text-amber-400">{f.severity}</span>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function TraceSessionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const traceId = decodeURIComponent(id);
  const [session, setSession] = useState<TraceSession | null>(null);
  const [advisory, setAdvisory] = useState<TraceAdvisory | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetch(`/api/trace/${encodeURIComponent(traceId)}/session`, { cache: "no-store" }).then((r) => r.json()).catch(() => null),
      fetch(`/api/trace/${encodeURIComponent(traceId)}/advisory`, { cache: "no-store" }).then((r) => r.json()).catch(() => null),
    ])
      .then(([s, a]) => { setSession(s); setAdvisory(a); })
      .finally(() => setLoading(false));
  }, [traceId]);

  return (
    <div className="flex h-full">
      <Sidebar />
      <main className="flex-1 flex flex-col overflow-hidden bg-zinc-950 font-mono text-[11px]">
        <header className="sticky top-0 z-10 flex items-center gap-3 px-4 h-9 border-b border-zinc-800 bg-zinc-950 shrink-0">
          <Link href="/pipeline" className="flex items-center gap-1 text-[10px] text-zinc-500 hover:text-amber-400 transition-colors">
            <ChevronLeft size={12} /> Pipeline
          </Link>
          <span className="text-zinc-700">/</span>
          <span className="text-[10px] text-amber-400 font-semibold tracking-wide uppercase">Diagnosis Session</span>
          <span className="text-[10px] text-zinc-500 font-mono truncate">{traceId}</span>
        </header>

        <div className="flex-1 overflow-auto px-4 py-4">
          {loading ? (
            <div className="flex items-center justify-center h-full text-zinc-700">
              <p className="text-[10px]">Loading session…</p>
            </div>
          ) : session && session.found ? (
            <div className="flex flex-col gap-4 max-w-3xl">
              {/* Final summary */}
              <div className="border border-zinc-800 rounded-lg bg-zinc-900/40 p-3 flex flex-col gap-2">
                <div className="flex items-center gap-2">
                  <FileSearch size={13} className="text-amber-400" />
                  <span className="text-[10px] font-bold uppercase tracking-widest text-zinc-200">Final Diagnosis</span>
                  <span className="ml-auto text-[8px] text-zinc-600">{session.total_turns} turns · confidence {(session.final.confidence * 100).toFixed(0)}%</span>
                </div>
                {session.final.root_cause && (
                  <p className="text-[11px] text-zinc-200 leading-relaxed">{session.final.root_cause}</p>
                )}
                {session.final.blast_radius && (
                  <p className="text-[9px] text-zinc-500"><span className="text-zinc-600 uppercase tracking-wider">Blast radius:</span> {session.final.blast_radius}</p>
                )}
                {session.final.remediation_steps.length > 0 && (
                  <div>
                    <span className="text-[8px] uppercase tracking-widest text-zinc-600">Remediation</span>
                    <ol className="list-decimal list-inside text-[10px] text-zinc-400 leading-relaxed">
                      {session.final.remediation_steps.map((s, i) => <li key={i}>{s}</li>)}
                    </ol>
                  </div>
                )}
                {session.degraded && (
                  <p className="flex items-center gap-1 text-[9px] text-amber-400">
                    <AlertCircle size={10} /> Degraded: {session.degraded_reason}
                  </p>
                )}
              </div>

              {/* Turns */}
              <div className="flex flex-col gap-2">
                <span className="text-[8px] uppercase tracking-widest text-zinc-600">Diagnosis turns</span>
                {session.turns.map((t) => <TurnCard key={t.turn} turn={t} />)}
              </div>
            </div>
          ) : advisory && advisory.found && advisory.advisory ? (
            <AdvisoryReport adv={advisory.advisory} />
          ) : (
            <div className="flex flex-col items-center justify-center h-full gap-3 text-center px-6">
              <Workflow size={24} className="text-zinc-700" />
              <p className="text-[11px] text-zinc-400">No diagnosis session or advisory stored for this trace</p>
              <p className="text-[9px] text-zinc-600 max-w-md leading-relaxed">
                The deep multi-turn diagnosis loop runs only for critical/high-urgency incidents;
                lower-urgency or suppressed traces may have no stored advisory. The full
                stage-by-stage flow is always on the pipeline view.
              </p>
              <Link
                href="/pipeline"
                className="inline-flex items-center gap-1 text-[10px] text-amber-400/80 hover:text-amber-300 transition-colors border border-zinc-800 rounded px-2.5 py-1 mt-1"
              >
                <Workflow size={11} /> Open pipeline view
              </Link>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
