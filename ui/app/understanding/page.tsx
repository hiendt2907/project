"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Sidebar } from "@/components/sidebar";
import { TenantSelector } from "@/components/tenant-selector";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { MermaidBlock, splitDiagramText } from "@/components/mermaid-diagram";
import { DiagramHistoryPanel } from "@/components/diagram-history";
import { Skeleton } from "@/components/ui/skeleton";
import {
  RefreshCw,
  Server,
  Boxes,
  CircleHelp,
  MessageCircleQuestion,
  Gauge,
  AlertTriangle,
  Send,
  Workflow,
  Radio,
} from "lucide-react";

interface SectionResult<T> {
  data: T | null;
  error: string | null;
}

interface EntitiesData {
  revision: number;
  hosts: string[];
  services: string[];
}

interface UnknownRecord {
  entity_type?: string;
  entity_id: string;
  facet: string;
  reason?: string;
  severity?: string;
  status: string;
}

interface QuestionRecord {
  question_id: string;
  entity_id: string;
  facet: string;
  text?: string;
  status: string;
}

interface ReadinessData {
  readiness: Record<string, unknown> | null;
}

interface DiagramData {
  version: number | null;
  mermaid: string | null;
}

interface AgentRecord {
  agent_id: string;
  tenant_id: string;
  hostname: string;
  version: string;
  last_seen: number;
  age_seconds: number;
  online: boolean;
}

interface UnderstandingData {
  entities: SectionResult<EntitiesData>;
  unknowns: SectionResult<{ unknowns: UnknownRecord[] }>;
  questions: SectionResult<{ questions: QuestionRecord[] }>;
  readiness: SectionResult<ReadinessData>;
  diagram: SectionResult<DiagramData>;
  agents: SectionResult<{ agents: AgentRecord[]; total: number }>;
}

interface FacetValueDto {
  state: string;
  value: unknown;
  evidence_refs: string[];
  source_types: string[];
  confidence: number;
}

interface CompetencyData {
  entity_id: string;
  facets: Record<string, FacetValueDto>;
  coverage: { coverage_pct?: number };
  critical_unknowns: string[];
  contradicted_facets: string[];
}

const STATE_STYLES: Record<string, string> = {
  VERIFIED: "bg-emerald-500/10 text-emerald-400 ring-emerald-500/30",
  CLAIMED: "bg-cyan-500/10 text-cyan-400 ring-cyan-500/30",
  OBSERVED: "bg-amber-500/10 text-amber-400 ring-amber-500/30",
  CONTRADICTED: "bg-rose-500/10 text-rose-400 ring-rose-500/30",
  STALE: "bg-orange-500/10 text-orange-400 ring-orange-500/30",
  UNKNOWN: "bg-zinc-700/30 text-zinc-400 ring-zinc-600/40",
  NOT_APPLICABLE: "bg-zinc-800/40 text-zinc-600 ring-zinc-700/40",
};

function StateBadge({ state }: { state: string }) {
  const style = STATE_STYLES[state] ?? STATE_STYLES.UNKNOWN;
  return (
    <span className={`inline-flex rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold ring-1 ring-inset ${style}`}>
      {state}
    </span>
  );
}

function SectionError({ error }: { error: string }) {
  return (
    <div className="flex items-center gap-2 rounded border border-rose-900/40 bg-rose-500/5 px-3 py-2 text-xs text-rose-400">
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
      {error}
    </div>
  );
}

function formatAge(seconds: number): string {
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function entityTypeOf(entityId: string): string {
  return entityId.startsWith("svc:") ? "service" : "host";
}

function UnderstandingPageInner() {
  const searchParams = useSearchParams();
  const tenant = searchParams.get("tenant") ?? "default";

  const [data, setData] = useState<UnderstandingData | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [competency, setCompetency] = useState<CompetencyData | null>(null);
  const [competencyError, setCompetencyError] = useState<string | null>(null);
  const [prevTenant, setPrevTenant] = useState(tenant);
  const [answeringId, setAnsweringId] = useState<string | null>(null);
  const [answerValue, setAnswerValue] = useState("");
  const [answeredBy, setAnsweredBy] = useState("");
  const [answerSubmitting, setAnswerSubmitting] = useState(false);
  const [answerError, setAnswerError] = useState<string | null>(null);
  const [answeredIds, setAnsweredIds] = useState<string[]>([]);
  const [showHistory, setShowHistory] = useState(false);

  if (tenant !== prevTenant) {
    setPrevTenant(tenant);
    setSelected(null);
    setCompetency(null);
    setAnsweringId(null);
    setAnswerError(null);
    setAnsweredIds([]);
    setShowHistory(false);
  }

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`/api/onboarding/understanding?tenant_id=${encodeURIComponent(tenant)}`, { cache: "no-store" });
      if (res.ok) setData(await res.json());
    } catch {
      // keep existing state; per-section errors render inside data
    } finally {
      setLoading(false);
    }
  }, [tenant]);

  useEffect(() => {
    load();
  }, [load]);

  const submitAnswer = useCallback(
    async (questionId: string) => {
      setAnswerSubmitting(true);
      setAnswerError(null);
      try {
        const res = await fetch("/api/onboarding/answer", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            question_id: questionId,
            answered_by: answeredBy,
            value: answerValue,
            tenant_id: tenant,
          }),
        });
        const body = (await res.json().catch(() => null)) as { error?: string } | null;
        if (!res.ok) {
          setAnswerError(body?.error ?? `HTTP ${res.status}`);
          return;
        }
        setAnsweredIds((prev) => [...prev, questionId]);
        setAnsweringId(null);
        setAnswerValue("");
        await load();
      } catch {
        setAnswerError("request failed");
      } finally {
        setAnswerSubmitting(false);
      }
    },
    [answeredBy, answerValue, tenant, load],
  );

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setCompetency(null);
    setCompetencyError(null);
    const params = new URLSearchParams({
      entity_type: entityTypeOf(selected),
      entity_id: selected,
      tenant_id: tenant,
    });
    fetch(`/api/onboarding/competency?${params.toString()}`, { cache: "no-store" })
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) {
          const body = (await res.json().catch(() => null)) as { error?: string } | null;
          setCompetencyError(body?.error ?? `HTTP ${res.status}`);
          return;
        }
        setCompetency((await res.json()) as CompetencyData);
      })
      .catch(() => {
        if (!cancelled) setCompetencyError("request failed");
      });
    return () => {
      cancelled = true;
    };
  }, [selected, tenant]);

  const entities = data?.entities;
  const unknowns = data?.unknowns.data?.unknowns ?? [];
  const openUnknowns = unknowns.filter((u) => u.status === "OPEN" || u.status === "QUESTION_EMITTED");
  const questions = data?.questions.data?.questions ?? [];
  const pendingQuestions = questions.filter((q) => q.status === "PENDING");
  const readiness = data?.readiness.data?.readiness ?? null;
  const agents = data?.agents?.data?.agents ?? [];
  const onlineAgents = agents.filter((a) => a.online);
  const diagram = data?.diagram?.data ?? null;
  const diagramSections = diagram?.mermaid ? splitDiagramText(diagram.mermaid) : [];

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        <header className="sticky top-0 z-10 flex h-16 items-center justify-between border-b border-zinc-800 bg-zinc-950/80 px-6 backdrop-blur">
          <div>
            <h1 className="text-base font-semibold text-zinc-100">System Understanding</h1>
            <p className="text-xs text-zinc-500">
              What Omni knows, claims, and still doesn&apos;t know about tenant <span className="text-zinc-300">{tenant}</span>
            </p>
          </div>
          <div className="flex items-center gap-3">
            <Suspense fallback={null}>
              <TenantSelector />
            </Suspense>
            <button
              onClick={load}
              className="flex items-center gap-1.5 rounded border border-zinc-700 px-2.5 py-1.5 text-xs text-zinc-400 hover:border-zinc-600 hover:text-zinc-100 transition-colors"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              Refresh
            </button>
          </div>
        </header>

        <div className="grid gap-4 p-6 lg:grid-cols-3">
          {/* Readiness */}
          <Card className="border-zinc-800 bg-zinc-900/60 lg:col-span-3">
            <CardHeader className="p-4 pb-2">
              <CardTitle className="flex items-center gap-2 text-sm text-zinc-100">
                <Gauge className="h-4 w-4 text-cyan-400" />
                Understanding Readiness
              </CardTitle>
            </CardHeader>
            <CardContent className="p-4 pt-0">
              {loading && !data ? (
                <Skeleton className="h-10 bg-zinc-800" />
              ) : data?.readiness.error ? (
                <SectionError error={data.readiness.error} />
              ) : readiness ? (
                <div className="flex flex-wrap gap-x-6 gap-y-2 font-mono text-xs">
                  {Object.entries(readiness).map(([k, v]) => (
                    <div key={k} className="flex items-center gap-2">
                      <span className="text-zinc-500">{k}</span>
                      <span className={v === true ? "text-emerald-400" : v === false ? "text-amber-400" : "text-zinc-200"}>
                        {String(v)}
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-zinc-500">No readiness record for this tenant yet.</p>
              )}
            </CardContent>
          </Card>

          {/* Remote agents — enrollment & heartbeat, operator-visible */}
          <Card className="border-zinc-800 bg-zinc-900/60 lg:col-span-3" data-testid="agents-card">
            <CardHeader className="p-4 pb-2">
              <CardTitle className="flex items-center justify-between text-sm text-zinc-100">
                <span className="flex items-center gap-2">
                  <Radio className="h-4 w-4 text-cyan-400" />
                  Remote Agents
                </span>
                {agents.length > 0 && (
                  <span className={`inline-flex rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold ring-1 ring-inset ${
                    onlineAgents.length === agents.length
                      ? "bg-emerald-500/10 text-emerald-400 ring-emerald-500/30"
                      : "bg-amber-500/10 text-amber-400 ring-amber-500/30"
                  }`}>
                    {onlineAgents.length}/{agents.length} online
                  </span>
                )}
              </CardTitle>
              <p className="text-[11px] leading-snug text-zinc-500">
                Collectors installed on this tenant&apos;s hosts. Each one reports what it sees back to
                Omni — if an agent goes offline, Omni stops learning about that host.
              </p>
            </CardHeader>
            <CardContent className="p-4 pt-1">
              {loading && !data ? (
                <Skeleton className="h-16 bg-zinc-800" />
              ) : data?.agents?.error ? (
                <SectionError error={data.agents.error} />
              ) : agents.length === 0 ? (
                <p className="text-xs text-zinc-500">
                  No agents enrolled for this tenant yet. Install the remote agent on a host to start
                  discovery — Omni cannot observe a host without one.
                </p>
              ) : (
                <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                  {agents.map((a) => (
                    <div
                      key={a.agent_id}
                      className="flex items-start gap-2.5 rounded border border-zinc-800 bg-zinc-900/40 px-3 py-2"
                    >
                      <span
                        className={`mt-1 h-2 w-2 shrink-0 rounded-full ${
                          a.online ? "bg-emerald-400" : "bg-rose-400"
                        }`}
                        aria-hidden
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center justify-between gap-2">
                          <span className="truncate text-xs font-medium text-zinc-200">
                            {a.hostname || a.agent_id}
                          </span>
                          <span
                            className={`inline-flex shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold ring-1 ring-inset ${
                              a.online
                                ? "bg-emerald-500/10 text-emerald-400 ring-emerald-500/30"
                                : "bg-rose-500/10 text-rose-400 ring-rose-500/30"
                            }`}
                          >
                            {a.online ? "Online" : "Offline"}
                          </span>
                        </div>
                        <p className="truncate font-mono text-[10px] text-zinc-600">{a.agent_id}</p>
                        <p className="text-[10px] text-zinc-500">
                          Last report {formatAge(a.age_seconds)}
                          {a.version && a.version !== "unknown" ? ` · v${a.version}` : ""}
                        </p>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          {/* System diagram (Mermaid) */}
          <Card className="border-zinc-800 bg-zinc-900/60 lg:col-span-3" data-testid="diagram-card">
            <CardHeader className="p-4 pb-2">
              <CardTitle className="flex items-center justify-between text-sm text-zinc-100">
                <span className="flex items-center gap-2">
                  <Workflow className="h-4 w-4 text-cyan-400" />
                  System Diagram
                </span>
                <span className="flex items-center gap-2">
                  {diagram?.version != null && (
                    <span className="font-mono text-[10px] text-zinc-500">v{diagram.version}</span>
                  )}
                  <button
                    onClick={() => setShowHistory((s) => !s)}
                    className={`rounded border px-2 py-0.5 text-[11px] transition-colors ${
                      showHistory
                        ? "border-cyan-500/40 bg-cyan-500/10 text-cyan-300"
                        : "border-zinc-700 text-zinc-400 hover:border-zinc-600 hover:text-zinc-100"
                    }`}
                  >
                    History
                  </button>
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="p-4 pt-0">
              {loading && !data ? (
                <Skeleton className="h-24 bg-zinc-800" />
              ) : data?.diagram?.error ? (
                <SectionError error={data.diagram.error} />
              ) : diagramSections.length === 0 ? (
                <p className="text-xs text-zinc-500">No diagram generated for this tenant yet.</p>
              ) : (
                <div className="grid gap-4 xl:grid-cols-3">
                  {diagramSections.map((section) => (
                    <div key={section.title}>
                      <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-widest text-zinc-600">
                        {section.title}
                      </p>
                      <MermaidBlock source={section.source} />
                    </div>
                  ))}
                </div>
              )}
              {showHistory && <DiagramHistoryPanel tenant={tenant} />}
            </CardContent>
          </Card>

          {/* Entities */}
          <Card className="border-zinc-800 bg-zinc-900/60">
            <CardHeader className="p-4 pb-2">
              <CardTitle className="flex items-center justify-between text-sm text-zinc-100">
                <span className="flex items-center gap-2">
                  <Server className="h-4 w-4 text-cyan-400" />
                  System Twin Entities
                </span>
                {entities?.data && (
                  <span className="font-mono text-[10px] text-zinc-500">rev {entities.data.revision}</span>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent className="max-h-[28rem] space-y-3 overflow-y-auto p-4 pt-0">
              {loading && !data ? (
                Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-8 bg-zinc-800" />)
              ) : entities?.error ? (
                <SectionError error={entities.error} />
              ) : (
                <>
                  {(["hosts", "services"] as const).map((group) => (
                    <div key={group}>
                      <p className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-widest text-zinc-600">
                        {group === "hosts" ? <Server className="h-3 w-3" /> : <Boxes className="h-3 w-3" />}
                        {group} ({entities?.data?.[group].length ?? 0})
                      </p>
                      <div className="space-y-1">
                        {(entities?.data?.[group] ?? []).map((id) => (
                          <button
                            key={id}
                            onClick={() => setSelected(id)}
                            className={`block w-full rounded border px-2.5 py-1.5 text-left font-mono text-xs transition-colors ${
                              selected === id
                                ? "border-cyan-500/40 bg-cyan-500/10 text-cyan-300"
                                : "border-zinc-800 bg-zinc-900/40 text-zinc-300 hover:border-zinc-600"
                            }`}
                          >
                            {id}
                          </button>
                        ))}
                      </div>
                    </div>
                  ))}
                  {entities?.data && entities.data.hosts.length === 0 && entities.data.services.length === 0 && (
                    <p className="text-xs text-zinc-500">System Twin is empty for this tenant.</p>
                  )}
                </>
              )}
            </CardContent>
          </Card>

          {/* Competency detail */}
          <Card className="border-zinc-800 bg-zinc-900/60 lg:col-span-2">
            <CardHeader className="p-4 pb-2">
              <CardTitle className="flex items-center justify-between text-sm text-zinc-100">
                <span>Competency Matrix{selected ? ` — ${selected}` : ""}</span>
                {competency?.coverage?.coverage_pct !== undefined && (
                  <span className="font-mono text-[10px] text-zinc-500">
                    coverage {Number(competency.coverage.coverage_pct).toFixed(0)}%
                  </span>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent className="max-h-[28rem] overflow-y-auto p-4 pt-0">
              {!selected ? (
                <p className="text-xs text-zinc-500">Select an entity to inspect facet-level knowledge state.</p>
              ) : competencyError ? (
                <SectionError error={competencyError} />
              ) : !competency ? (
                <Skeleton className="h-32 bg-zinc-800" />
              ) : (
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-zinc-800 text-[10px] uppercase tracking-widest text-zinc-600">
                      <th className="py-1.5 pr-2 font-semibold">Facet</th>
                      <th className="py-1.5 pr-2 font-semibold">State</th>
                      <th className="py-1.5 pr-2 font-semibold">Value</th>
                      <th className="py-1.5 pr-2 font-semibold">Conf</th>
                      <th className="py-1.5 font-semibold">Evidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(competency.facets).map(([name, fv]) => (
                      <tr key={name} className="border-b border-zinc-800/60 align-top">
                        <td className="py-2 pr-2 font-mono text-zinc-300">{name}</td>
                        <td className="py-2 pr-2"><StateBadge state={fv.state} /></td>
                        <td className="max-w-48 truncate py-2 pr-2 font-mono text-zinc-400" title={String(fv.value ?? "")}>
                          {fv.value === null || fv.value === undefined ? "—" : String(fv.value)}
                        </td>
                        <td className="py-2 pr-2 font-mono text-zinc-500">{fv.confidence.toFixed(2)}</td>
                        <td className="py-2 font-mono text-[10px] leading-relaxed text-zinc-500">
                          {fv.evidence_refs.length ? fv.evidence_refs.join(", ") : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </CardContent>
          </Card>

          {/* Unknowns */}
          <Card className="border-zinc-800 bg-zinc-900/60 lg:col-span-2">
            <CardHeader className="p-4 pb-2">
              <CardTitle className="flex items-center gap-2 text-sm text-zinc-100">
                <CircleHelp className="h-4 w-4 text-amber-400" />
                Open Unknowns ({openUnknowns.length})
              </CardTitle>
            </CardHeader>
            <CardContent className="max-h-80 overflow-y-auto p-4 pt-0">
              {loading && !data ? (
                <Skeleton className="h-20 bg-zinc-800" />
              ) : data?.unknowns.error ? (
                <SectionError error={data.unknowns.error} />
              ) : openUnknowns.length === 0 ? (
                <p className="text-xs text-zinc-500">No open unknowns.</p>
              ) : (
                <div className="space-y-1.5">
                  {openUnknowns.map((u, i) => (
                    <div key={`${u.entity_id}-${u.facet}-${i}`} className="flex items-center gap-2 rounded border border-zinc-800 bg-zinc-900/40 px-2.5 py-1.5 font-mono text-xs">
                      <span className={`inline-flex rounded px-1.5 py-0.5 text-[10px] font-semibold ring-1 ring-inset ${
                        u.severity === "high" ? "bg-rose-500/10 text-rose-400 ring-rose-500/30" : "bg-amber-500/10 text-amber-400 ring-amber-500/30"
                      }`}>
                        {u.severity ?? "medium"}
                      </span>
                      <button onClick={() => setSelected(u.entity_id)} className="text-cyan-400 hover:underline">{u.entity_id}</button>
                      <span className="text-zinc-300">{u.facet}</span>
                      <span className="ml-auto text-[10px] text-zinc-600">{u.status}</span>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          {/* Questions */}
          <Card className="border-zinc-800 bg-zinc-900/60">
            <CardHeader className="p-4 pb-2">
              <CardTitle className="flex items-center gap-2 text-sm text-zinc-100">
                <MessageCircleQuestion className="h-4 w-4 text-cyan-400" />
                Questions ({pendingQuestions.length} pending)
              </CardTitle>
            </CardHeader>
            <CardContent className="max-h-80 overflow-y-auto p-4 pt-0">
              {loading && !data ? (
                <Skeleton className="h-20 bg-zinc-800" />
              ) : data?.questions.error ? (
                <SectionError error={data.questions.error} />
              ) : questions.length === 0 ? (
                <p className="text-xs text-zinc-500">No questions generated yet.</p>
              ) : (
                <div className="space-y-1.5">
                  {questions.map((q) => (
                    <div key={q.question_id} className="rounded border border-zinc-800 bg-zinc-900/40 px-2.5 py-1.5">
                      <div className="flex items-center gap-2 font-mono text-xs">
                        <span className="text-zinc-300">{q.entity_id}</span>
                        <span className="text-zinc-500">{q.facet}</span>
                        <span className={`ml-auto text-[10px] ${q.status === "PENDING" ? "text-amber-400" : "text-emerald-400"}`}>
                          {answeredIds.includes(q.question_id) ? "ANSWERED" : q.status}
                        </span>
                      </div>
                      {q.text && <p className="mt-0.5 text-[11px] leading-snug text-zinc-500">{q.text}</p>}
                      {q.status === "PENDING" && !answeredIds.includes(q.question_id) && (
                        answeringId === q.question_id ? (
                          <form
                            className="mt-1.5 space-y-1.5"
                            onSubmit={(e) => {
                              e.preventDefault();
                              submitAnswer(q.question_id);
                            }}
                          >
                            <input
                              value={answeredBy}
                              onChange={(e) => setAnsweredBy(e.target.value)}
                              placeholder="Your name / role"
                              maxLength={120}
                              required
                              className="w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs text-zinc-200 placeholder:text-zinc-600 focus:border-cyan-500/50 focus:outline-none"
                            />
                            <textarea
                              value={answerValue}
                              onChange={(e) => setAnswerValue(e.target.value)}
                              placeholder="Answer (becomes a CLAIMED fact, verified later by machine evidence)"
                              maxLength={500}
                              required
                              rows={2}
                              className="w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs text-zinc-200 placeholder:text-zinc-600 focus:border-cyan-500/50 focus:outline-none"
                            />
                            {answerError && <SectionError error={answerError} />}
                            <div className="flex items-center gap-2">
                              <button
                                type="submit"
                                disabled={answerSubmitting || !answeredBy.trim() || !answerValue.trim()}
                                className="flex items-center gap-1.5 rounded bg-cyan-600/90 px-2.5 py-1 text-xs font-medium text-white transition-colors hover:bg-cyan-500 disabled:cursor-not-allowed disabled:opacity-40"
                              >
                                <Send className="h-3 w-3" />
                                {answerSubmitting ? "Submitting…" : "Submit answer"}
                              </button>
                              <button
                                type="button"
                                onClick={() => {
                                  setAnsweringId(null);
                                  setAnswerError(null);
                                }}
                                className="text-xs text-zinc-500 hover:text-zinc-300"
                              >
                                Cancel
                              </button>
                            </div>
                          </form>
                        ) : (
                          <button
                            onClick={() => {
                              setAnsweringId(q.question_id);
                              setAnswerValue("");
                              setAnswerError(null);
                            }}
                            className="mt-1.5 rounded border border-cyan-500/30 px-2 py-0.5 text-[11px] text-cyan-400 transition-colors hover:bg-cyan-500/10"
                          >
                            Answer
                          </button>
                        )
                      )}
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </main>
    </div>
  );
}

export default function UnderstandingPage() {
  return (
    <Suspense fallback={null}>
      <UnderstandingPageInner />
    </Suspense>
  );
}
