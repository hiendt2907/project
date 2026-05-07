"use client";

import { useEffect, useState } from "react";
import { Sidebar } from "@/components/sidebar";
import { DiagnosticLanes } from "@/components/diagnostic-lanes";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  AreaChart,
  Area,
  LineChart,
  Line,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  Legend,
} from "recharts";
import {
  Activity,
  Zap,
  Database,
  BookOpen,
  GitBranch,
  ShieldAlert,
  CheckCircle2,
  AlertTriangle,
  RefreshCw,
  Cpu,
  Clock,
  Network,
} from "lucide-react";

type SiemOverview = {
  generated_at: string;
  source: "prometheus" | "mock";
  ingestion: {
    total_last_24h: number;
    rate_per_min: number;
    by_severity: { critical: number; high: number; medium: number; low: number; info: number };
    by_category: { name: string; count: number }[];
    by_tenant: { tenant: string; count: number }[];
    timeline_24h: { hour: string; events: number; incidents: number }[];
  };
  correlation: {
    chains_detected_24h: number;
    active_windows: number;
    chains_by_category: { category: string; count: number }[];
    last_chain_trace_id: string;
    last_chain_detected_at: string;
  };
  llm: {
    total_calls_24h: number;
    success_rate: number;
    failure_count: number;
    latency_p50_ms: number;
    latency_p95_ms: number;
    latency_p99_ms: number;
    tokens_in_total: number;
    tokens_out_total: number;
    by_model: { model: string; calls: number; avg_latency_ms: number; tokens: number; failures: number }[];
    latency_timeline: { hour: string; p50: number; p95: number }[];
    last_call_trace: string;
  };
  rag: {
    queries_24h: number;
    cache_hits: number;
    cache_misses: number;
    cache_hit_ratio: number;
    avg_query_latency_ms: number;
    by_collection: { name: string; queries: number; hit_ratio: number; vectors: number }[];
    top_queries: { query: string; count: number; avg_distance: number }[];
  };
  playbook: {
    matches_24h: number;
    auto_executed: number;
    hitl_gated: number;
    no_match: number;
    by_playbook: { id: string; name: string; matches: number; success: number; failures: number }[];
  };
  hitl: {
    pending: number;
    approved_24h: number;
    rejected_24h: number;
    timed_out_24h: number;
    avg_approval_time_sec: number;
    queue: { incident_id: string; category: string; severity: string; waiting_sec: number; trace_id: string }[];
  };
  pipeline: {
    kafka_lag: { topic: string; group: string; lag: number }[];
    redis_ops_per_sec: number;
    redis_memory_used_bytes: number;
    redis_memory_max_bytes: number;
    workers: { role: string; replicas: number; ready: number; last_heartbeat_age_sec: number }[];
  };
  diagnostic_lanes: {
    sys_resource: { status: "ok" | "warn" | "critical"; z_cpu: number; z_mem: number; baseline_age_sec: number; anomaly_triggered: boolean };
    sys_hard_fail: { status: "ok" | "warn" | "critical"; crash_loops: number; broken_specs: number; advisory_count_24h: number };
    app_http: { status: "ok" | "warn" | "critical"; surge_triggered: boolean; dominant_error_class: "5xx" | "429" | "499" | "401" | "403" | "none"; error_rate_pct: number; sigma_bypass: boolean };
    siem_security: { status: "ok" | "warn" | "critical"; active_incidents: number; kill_chain_stage: string; pipeline_healthy: boolean; forecast_1h_severity: string };
  };
};

type Accent = "cyan" | "emerald" | "amber" | "violet" | "rose" | "sky";

const accentClass: Record<Accent, string> = {
  cyan: "text-cyan-400 bg-cyan-500/10 ring-cyan-500/20",
  emerald: "text-emerald-400 bg-emerald-500/10 ring-emerald-500/20",
  amber: "text-amber-400 bg-amber-500/10 ring-amber-500/20",
  violet: "text-violet-400 bg-violet-500/10 ring-violet-500/20",
  rose: "text-rose-400 bg-rose-500/10 ring-rose-500/20",
  sky: "text-sky-400 bg-sky-500/10 ring-sky-500/20",
};

const severityColor: Record<string, string> = {
  critical: "#ef4444",
  high: "#f97316",
  medium: "#f59e0b",
  low: "#22d3ee",
  info: "#71717a",
};

const severityBadge: Record<string, string> = {
  critical: "bg-rose-500/15 text-rose-400 border-rose-500/30",
  high: "bg-orange-500/15 text-orange-400 border-orange-500/30",
  medium: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  low: "bg-sky-500/15 text-sky-400 border-sky-500/30",
  info: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
};

function bytesToHuman(n: number): string {
  if (n > 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GiB`;
  if (n > 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MiB`;
  return `${(n / 1024).toFixed(0)} KiB`;
}

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toString();
}

function StatCard({
  label,
  value,
  sub,
  icon: Icon,
  accent = "cyan",
}: {
  label: string;
  value: string | number;
  sub?: string;
  icon: React.ElementType;
  accent?: Accent;
}) {
  return (
    <Card className="border-zinc-800 bg-zinc-900/60">
      <CardContent className="flex items-center gap-3 p-4">
        <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ring-1 ${accentClass[accent]}`}>
          <Icon className="h-4 w-4" />
        </div>
        <div className="min-w-0">
          <p className="text-[10px] uppercase tracking-widest text-zinc-500">{label}</p>
          <p className="mt-0.5 font-mono text-xl font-bold text-zinc-100 truncate">{value}</p>
          {sub && <p className="mt-0.5 text-[11px] text-zinc-500 truncate">{sub}</p>}
        </div>
      </CardContent>
    </Card>
  );
}

function Panel({
  title,
  hint,
  children,
  className = "",
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Card className={`border-zinc-800 bg-zinc-900/60 ${className}`}>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm font-medium text-zinc-300">{title}</CardTitle>
          {hint && <span className="text-[10px] uppercase tracking-widest text-zinc-600">{hint}</span>}
        </div>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

const tooltipStyle = {
  background: "#09090b",
  border: "1px solid #27272a",
  borderRadius: 8,
  fontSize: 12,
  color: "#e4e4e7",
};

export default function SiemDashboardPage() {
  const [data, setData] = useState<SiemOverview | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date>(new Date());

  async function load() {
    const res = await fetch("/api/siem/overview", { cache: "no-store" });
    const json = (await res.json()) as SiemOverview;
    setData(json);
    setLastRefresh(new Date());
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 15_000);
    return () => clearInterval(t);
  }, []);

  const severityData = data
    ? Object.entries(data.ingestion.by_severity).map(([k, v]) => ({ name: k, value: v }))
    : [];

  const redisPct = data
    ? Math.round((data.pipeline.redis_memory_used_bytes / data.pipeline.redis_memory_max_bytes) * 100)
    : 0;

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        <header className="sticky top-0 z-10 flex h-16 items-center justify-between border-b border-zinc-800 bg-zinc-950/80 px-6 backdrop-blur">
          <div>
            <h1 className="text-base font-semibold text-zinc-100">Smart SIEM Operational Dashboard</h1>
            <p className="text-xs text-zinc-500">
              Ingestion · Correlation · LLM · RAG · Playbook · HITL · Pipeline
            </p>
          </div>
          <div className="flex items-center gap-3 text-xs text-zinc-600">
            <span className="flex items-center gap-1">
              <span className={`h-1.5 w-1.5 rounded-full ${data?.source === "prometheus" ? "bg-emerald-400" : "bg-amber-400"}`} />
              {data?.source === "prometheus" ? "live metrics" : "mock fallback"}
            </span>
            <span className="flex items-center gap-1">
              <RefreshCw className="h-3 w-3" />
              {lastRefresh.toLocaleTimeString()}
            </span>
          </div>
        </header>

        <div className="space-y-6 p-6">
          {/* Row 1 — 6 stat cards */}
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            {data ? (
              <>
                <StatCard
                  label="Events · 24h"
                  value={compact(data.ingestion.total_last_24h)}
                  sub={`${data.ingestion.rate_per_min.toFixed(1)} / min`}
                  icon={Activity}
                  accent="cyan"
                />
                <StatCard
                  label="Chains Detected"
                  value={data.correlation.chains_detected_24h}
                  sub={`${data.correlation.active_windows} active windows`}
                  icon={GitBranch}
                  accent="violet"
                />
                <StatCard
                  label="LLM Calls · 24h"
                  value={compact(data.llm.total_calls_24h)}
                  sub={`${data.llm.success_rate.toFixed(1)}% ok · p95 ${data.llm.latency_p95_ms}ms`}
                  icon={Cpu}
                  accent="sky"
                />
                <StatCard
                  label="RAG Queries"
                  value={compact(data.rag.queries_24h)}
                  sub={`${data.rag.cache_hit_ratio.toFixed(1)}% cache hit`}
                  icon={BookOpen}
                  accent="emerald"
                />
                <StatCard
                  label="Playbook Matches"
                  value={data.playbook.matches_24h}
                  sub={`${data.playbook.auto_executed} auto · ${data.playbook.hitl_gated} HITL`}
                  icon={Zap}
                  accent="amber"
                />
                <StatCard
                  label="HITL Pending"
                  value={data.hitl.pending}
                  sub={`${data.hitl.approved_24h} approved · ${data.hitl.rejected_24h} rejected`}
                  icon={ShieldAlert}
                  accent="rose"
                />
              </>
            ) : (
              Array.from({ length: 6 }).map((_, i) => (
                <Card key={i} className="border-zinc-800 bg-zinc-900/60">
                  <CardContent className="p-4">
                    <Skeleton className="h-14 w-full bg-zinc-800" />
                  </CardContent>
                </Card>
              ))
            )}
          </div>

          {/* Row 2 — Ingestion timeline + severity + categories */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-6">
            <Panel title="Event Ingestion · 24h" hint="events / incidents" className="lg:col-span-3">
              {data ? (
                <ResponsiveContainer width="100%" height={220}>
                  <AreaChart data={data.ingestion.timeline_24h} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
                    <XAxis dataKey="hour" tick={{ fontSize: 10, fill: "#71717a" }} tickLine={false} axisLine={false} interval={3} />
                    <YAxis tick={{ fontSize: 10, fill: "#71717a" }} tickLine={false} axisLine={false} />
                    <Tooltip contentStyle={tooltipStyle} />
                    <Area type="monotone" dataKey="events" stroke="#22d3ee" fill="#22d3ee22" strokeWidth={1.5} name="events" />
                    <Area type="monotone" dataKey="incidents" stroke="#a78bfa" fill="#a78bfa33" strokeWidth={1.5} name="incidents" />
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <Skeleton className="h-56 w-full bg-zinc-800" />
              )}
            </Panel>

            <Panel title="Severity Distribution" className="lg:col-span-2">
              {data ? (
                <ResponsiveContainer width="100%" height={220}>
                  <PieChart>
                    <Pie data={severityData} dataKey="value" nameKey="name" cx="50%" cy="50%" innerRadius={48} outerRadius={84} stroke="#18181b" strokeWidth={2}>
                      {severityData.map((d) => (
                        <Cell key={d.name} fill={severityColor[d.name] ?? "#71717a"} />
                      ))}
                    </Pie>
                    <Tooltip contentStyle={tooltipStyle} />
                    <Legend wrapperStyle={{ fontSize: 11, color: "#a1a1aa" }} />
                  </PieChart>
                </ResponsiveContainer>
              ) : (
                <Skeleton className="h-56 w-full bg-zinc-800" />
              )}
            </Panel>

            <Panel title="Top Categories" className="lg:col-span-1">
              {data ? (
                <div className="space-y-2">
                  {data.ingestion.by_category.slice(0, 6).map((c) => (
                    <div key={c.name} className="space-y-1">
                      <div className="flex items-center justify-between text-[11px]">
                        <span className="truncate font-mono text-zinc-400">{c.name}</span>
                        <span className="font-mono text-cyan-400">{compact(c.count)}</span>
                      </div>
                      <div className="h-1 w-full rounded-full bg-zinc-800">
                        <div
                          className="h-1 rounded-full bg-cyan-500/70"
                          style={{
                            width: `${(c.count / (data.ingestion.by_category[0]?.count || 1)) * 100}%`,
                          }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <Skeleton className="h-56 w-full bg-zinc-800" />
              )}
            </Panel>
          </div>

          {/* Row 3 — LLM timeline + model table */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
            <Panel title="LLM Latency · 24h" hint="p50 · p95 (ms)" className="lg:col-span-3">
              {data ? (
                <ResponsiveContainer width="100%" height={220}>
                  <LineChart data={data.llm.latency_timeline} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
                    <XAxis dataKey="hour" tick={{ fontSize: 10, fill: "#71717a" }} tickLine={false} axisLine={false} interval={3} />
                    <YAxis tick={{ fontSize: 10, fill: "#71717a" }} tickLine={false} axisLine={false} />
                    <Tooltip contentStyle={tooltipStyle} />
                    <Line type="monotone" dataKey="p50" stroke="#22d3ee" strokeWidth={1.5} dot={false} name="p50" />
                    <Line type="monotone" dataKey="p95" stroke="#f59e0b" strokeWidth={1.5} dot={false} name="p95" />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <Skeleton className="h-56 w-full bg-zinc-800" />
              )}
            </Panel>

            <Panel title="By Model" hint="calls · avg ms · tokens · fail" className="lg:col-span-2">
              {data ? (
                <div className="space-y-3">
                  {data.llm.by_model.map((m) => {
                    const okRate = ((m.calls - m.failures) / m.calls) * 100;
                    return (
                      <div key={m.model} className="space-y-1.5 rounded-lg border border-zinc-800/60 bg-zinc-950/40 p-2.5">
                        <div className="flex items-center justify-between text-[11px]">
                          <span className="truncate font-mono text-zinc-300">{m.model}</span>
                          <span className="font-mono text-emerald-400">{okRate.toFixed(1)}%</span>
                        </div>
                        <div className="flex items-center justify-between font-mono text-[10px] text-zinc-500">
                          <span>{compact(m.calls)} calls</span>
                          <span>{m.avg_latency_ms}ms</span>
                          <span>{compact(m.tokens)} tok</span>
                          <span className={m.failures > 0 ? "text-amber-400" : "text-zinc-600"}>
                            {m.failures} fail
                          </span>
                        </div>
                      </div>
                    );
                  })}
                  <div className="mt-2 flex items-center justify-between border-t border-zinc-800 pt-2 text-[10px] text-zinc-500">
                    <span>tokens in · {compact(data.llm.tokens_in_total)}</span>
                    <span>out · {compact(data.llm.tokens_out_total)}</span>
                  </div>
                </div>
              ) : (
                <Skeleton className="h-56 w-full bg-zinc-800" />
              )}
            </Panel>
          </div>

          {/* Row 4 — RAG collections + top queries */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
            <Panel title="RAG Collections" hint="queries · hit ratio · vectors" className="lg:col-span-3">
              {data ? (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-zinc-800 text-left text-[10px] uppercase tracking-widest text-zinc-500">
                        <th className="pb-2 pr-4">Collection</th>
                        <th className="pb-2 pr-4">Queries</th>
                        <th className="pb-2 pr-4">Hit %</th>
                        <th className="pb-2">Vectors</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-zinc-800/60 font-mono">
                      {data.rag.by_collection.map((c) => (
                        <tr key={c.name} className="hover:bg-zinc-800/30">
                          <td className="py-2 pr-4 text-zinc-300">{c.name}</td>
                          <td className="py-2 pr-4 text-zinc-400">{compact(c.queries)}</td>
                          <td className="py-2 pr-4">
                            <div className="flex items-center gap-2">
                              <div className="h-1 w-16 rounded-full bg-zinc-800">
                                <div
                                  className="h-1 rounded-full bg-emerald-500/70"
                                  style={{ width: `${Math.min(c.hit_ratio, 100)}%` }}
                                />
                              </div>
                              <span className="text-emerald-400">{c.hit_ratio.toFixed(1)}%</span>
                            </div>
                          </td>
                          <td className="py-2 text-zinc-400">{compact(c.vectors)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="mt-3 flex items-center justify-between border-t border-zinc-800 pt-2 text-[10px] text-zinc-500">
                    <span>
                      avg query latency · <span className="font-mono text-zinc-300">{data.rag.avg_query_latency_ms}ms</span>
                    </span>
                    <span>
                      cache hits · <span className="font-mono text-emerald-400">{compact(data.rag.cache_hits)}</span> /
                      misses · <span className="font-mono text-amber-400">{compact(data.rag.cache_misses)}</span>
                    </span>
                  </div>
                </div>
              ) : (
                <Skeleton className="h-48 w-full bg-zinc-800" />
              )}
            </Panel>

            <Panel title="Top Queries" hint="24h" className="lg:col-span-2">
              {data ? (
                <ol className="space-y-2">
                  {data.rag.top_queries.map((q, i) => (
                    <li key={i} className="flex items-start gap-2 rounded border border-zinc-800/60 bg-zinc-950/40 p-2">
                      <span className="font-mono text-[10px] text-zinc-600">{String(i + 1).padStart(2, "0")}</span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-[11px] text-zinc-300">{q.query}</p>
                        <p className="mt-0.5 font-mono text-[10px] text-zinc-500">
                          {q.count} hits · dist {q.avg_distance.toFixed(3)}
                        </p>
                      </div>
                    </li>
                  ))}
                </ol>
              ) : (
                <Skeleton className="h-48 w-full bg-zinc-800" />
              )}
            </Panel>
          </div>

          {/* Row 5 — Playbook table + HITL queue */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
            <Panel title="Playbook Performance" hint="matches · success · fail" className="lg:col-span-3">
              {data ? (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-zinc-800 text-left text-[10px] uppercase tracking-widest text-zinc-500">
                        <th className="pb-2 pr-4">ID</th>
                        <th className="pb-2 pr-4">Name</th>
                        <th className="pb-2 pr-4">Matches</th>
                        <th className="pb-2 pr-4">Success %</th>
                        <th className="pb-2">Fail</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-zinc-800/60 font-mono">
                      {data.playbook.by_playbook.map((pb) => {
                        const rate = (pb.success / Math.max(pb.matches, 1)) * 100;
                        return (
                          <tr key={pb.id} className="hover:bg-zinc-800/30">
                            <td className="py-2 pr-4 text-zinc-500">{pb.id}</td>
                            <td className="py-2 pr-4 text-zinc-300">{pb.name}</td>
                            <td className="py-2 pr-4 text-zinc-400">{pb.matches}</td>
                            <td className="py-2 pr-4">
                              <span className={rate >= 95 ? "text-emerald-400" : rate >= 85 ? "text-amber-400" : "text-rose-400"}>
                                {rate.toFixed(1)}%
                              </span>
                            </td>
                            <td className={`py-2 ${pb.failures > 0 ? "text-rose-400" : "text-zinc-600"}`}>{pb.failures}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  <div className="mt-3 flex items-center justify-between border-t border-zinc-800 pt-2 text-[10px] text-zinc-500">
                    <span>
                      auto · <span className="font-mono text-emerald-400">{data.playbook.auto_executed}</span>
                    </span>
                    <span>
                      hitl-gated · <span className="font-mono text-amber-400">{data.playbook.hitl_gated}</span>
                    </span>
                    <span>
                      no-match · <span className="font-mono text-rose-400">{data.playbook.no_match}</span>
                    </span>
                  </div>
                </div>
              ) : (
                <Skeleton className="h-48 w-full bg-zinc-800" />
              )}
            </Panel>

            <Panel title="HITL Queue" hint="awaiting approval" className="lg:col-span-2">
              {data ? (
                <div className="space-y-2">
                  {data.hitl.queue.length === 0 ? (
                    <p className="flex items-center gap-2 text-xs text-emerald-400">
                      <CheckCircle2 className="h-3.5 w-3.5" /> queue clear
                    </p>
                  ) : (
                    data.hitl.queue.map((inc) => (
                      <div
                        key={inc.incident_id}
                        className="rounded border border-zinc-800/60 bg-zinc-950/40 p-2.5"
                      >
                        <div className="flex items-center justify-between">
                          <span className="font-mono text-[11px] text-zinc-300">{inc.incident_id}</span>
                          <span
                            className={`rounded border px-1.5 py-0.5 text-[9px] uppercase tracking-wide ${severityBadge[inc.severity] ?? severityBadge.info}`}
                          >
                            {inc.severity}
                          </span>
                        </div>
                        <div className="mt-1 flex items-center justify-between font-mono text-[10px] text-zinc-500">
                          <span>{inc.category}</span>
                          <span className="flex items-center gap-1 text-amber-400">
                            <Clock className="h-3 w-3" />
                            {Math.floor(inc.waiting_sec / 60)}m{inc.waiting_sec % 60}s
                          </span>
                        </div>
                      </div>
                    ))
                  )}
                  <div className="mt-2 grid grid-cols-3 gap-2 border-t border-zinc-800 pt-2 text-center text-[10px] text-zinc-500">
                    <div>
                      <p className="font-mono text-emerald-400">{data.hitl.approved_24h}</p>
                      <p>approved</p>
                    </div>
                    <div>
                      <p className="font-mono text-rose-400">{data.hitl.rejected_24h}</p>
                      <p>rejected</p>
                    </div>
                    <div>
                      <p className="font-mono text-amber-400">{data.hitl.timed_out_24h}</p>
                      <p>timed out</p>
                    </div>
                  </div>
                </div>
              ) : (
                <Skeleton className="h-48 w-full bg-zinc-800" />
              )}
            </Panel>
          </div>

          {/* Row 6 — Pipeline: Kafka + Workers + Redis */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
            <Panel title="Kafka Consumer Lag" hint="events behind" className="lg:col-span-2">
              {data ? (
                <ResponsiveContainer width="100%" height={200}>
                  <BarChart data={data.pipeline.kafka_lag} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
                    <XAxis dataKey="topic" tick={{ fontSize: 9, fill: "#71717a" }} tickLine={false} axisLine={false} />
                    <YAxis tick={{ fontSize: 10, fill: "#71717a" }} tickLine={false} axisLine={false} allowDecimals={false} />
                    <Tooltip contentStyle={tooltipStyle} />
                    <Bar dataKey="lag" fill="#22d3ee" radius={[3, 3, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <Skeleton className="h-48 w-full bg-zinc-800" />
              )}
            </Panel>

            <Panel title="Worker Heartbeats" hint="replicas · ready · age" className="lg:col-span-2">
              {data ? (
                <div className="grid grid-cols-2 gap-2">
                  {data.pipeline.workers.map((w) => {
                    const healthy = w.ready === w.replicas && w.last_heartbeat_age_sec < 30;
                    return (
                      <div
                        key={w.role}
                        className="rounded border border-zinc-800/60 bg-zinc-950/40 p-2"
                      >
                        <div className="flex items-center justify-between">
                          <span className="font-mono text-[11px] text-zinc-300">{w.role}</span>
                          {healthy ? (
                            <CheckCircle2 className="h-3 w-3 text-emerald-400" />
                          ) : (
                            <AlertTriangle className="h-3 w-3 text-amber-400" />
                          )}
                        </div>
                        <p className="mt-1 font-mono text-[10px] text-zinc-500">
                          {w.ready}/{w.replicas} · hb {w.last_heartbeat_age_sec}s
                        </p>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <Skeleton className="h-48 w-full bg-zinc-800" />
              )}
            </Panel>

            <Panel title="Redis Stack" hint="ops · memory" className="lg:col-span-1">
              {data ? (
                <div className="space-y-3">
                  <div>
                    <p className="text-[10px] uppercase tracking-widest text-zinc-500">Ops / sec</p>
                    <p className="mt-0.5 flex items-baseline gap-1 font-mono">
                      <span className="text-2xl text-cyan-400">{data.pipeline.redis_ops_per_sec}</span>
                      <Network className="h-3 w-3 text-zinc-600" />
                    </p>
                  </div>
                  <div>
                    <p className="text-[10px] uppercase tracking-widest text-zinc-500">Memory</p>
                    <p className="mt-0.5 font-mono text-sm text-zinc-300">
                      {bytesToHuman(data.pipeline.redis_memory_used_bytes)}
                    </p>
                    <div className="mt-1.5 h-1.5 w-full rounded-full bg-zinc-800">
                      <div
                        className={`h-1.5 rounded-full ${redisPct > 85 ? "bg-rose-500" : redisPct > 70 ? "bg-amber-500" : "bg-emerald-500"}`}
                        style={{ width: `${redisPct}%` }}
                      />
                    </div>
                    <p className="mt-1 font-mono text-[10px] text-zinc-500">
                      {redisPct}% of {bytesToHuman(data.pipeline.redis_memory_max_bytes)}
                    </p>
                  </div>
                  <div className="flex items-center gap-1 border-t border-zinc-800 pt-2 text-[10px] text-zinc-600">
                    <Database className="h-3 w-3" />
                    HNSW vector + semantic cache
                  </div>
                </div>
              ) : (
                <Skeleton className="h-48 w-full bg-zinc-800" />
              )}
            </Panel>
          </div>

          {/* Row 7 — Diagnostic Lanes */}
          <section>
            <h2 className="mb-3 text-sm font-semibold text-zinc-400 uppercase tracking-wider">Diagnostic Lanes</h2>
            <DiagnosticLanes data={data?.diagnostic_lanes ?? null} />
          </section>

          {data && (
            <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] text-zinc-600">
              <span>
                last chain · <span className="font-mono text-zinc-400">{data.correlation.last_chain_trace_id}</span>
              </span>
              <span>
                last LLM trace · <span className="font-mono text-zinc-400">{data.llm.last_call_trace}</span>
              </span>
              <span>generated {new Date(data.generated_at).toLocaleString()}</span>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
