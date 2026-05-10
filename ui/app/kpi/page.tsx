"use client";

import { useEffect, useState, useCallback } from "react";
import { Sidebar } from "@/components/sidebar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
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
  RadialBarChart,
  RadialBar,
  Legend,
} from "recharts";
import {
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Clock,
  RefreshCw,
  TrendingUp,
  Activity,
  ShieldAlert,
} from "lucide-react";

type TrendItem = { lane: string; detected: number; resolved: number };

type KpiData = {
  generated_at: string;
  window: string;
  source: "gateway" | "mock";
  advisory: {
    accepted: number;
    rejected: number;
    total: number;
    acceptance_rate: number | null;
  };
  execution: {
    total_executed: number;
    false_positive: number;
    false_positive_rate: number | null;
  };
  trend: TrendItem[];
};

const LANE_COLORS: Record<string, string> = {
  SYS_RESOURCE: "#22d3ee",
  SYS_HARD_FAIL: "#f87171",
  APP_HTTP: "#fb923c",
  SIEM_SECURITY: "#a78bfa",
};

const LANE_LABELS: Record<string, string> = {
  SYS_RESOURCE: "Resource",
  SYS_HARD_FAIL: "Hard Fail",
  APP_HTTP: "HTTP",
  SIEM_SECURITY: "SIEM",
};

function pct(v: number | null | undefined) {
  if (v == null) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function StatCard({
  title,
  value,
  sub,
  icon: Icon,
  color,
}: {
  title: string;
  value: string;
  sub: string;
  icon: React.ElementType;
  color: string;
}) {
  return (
    <Card className="bg-zinc-900/60 border-zinc-800">
      <CardContent className="p-5">
        <div className="flex items-start justify-between">
          <div>
            <p className="text-xs text-zinc-500 uppercase tracking-wider mb-1">{title}</p>
            <p className={`text-2xl font-bold ${color}`}>{value}</p>
            <p className="text-xs text-zinc-500 mt-1">{sub}</p>
          </div>
          <div className={`p-2 rounded-md bg-zinc-800`}>
            <Icon className={`h-5 w-5 ${color}`} />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export default function KpiPage() {
  const [data, setData] = useState<KpiData | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/kpi");
      if (res.ok) {
        setData(await res.json());
        setLastRefresh(new Date());
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 30_000);
    return () => clearInterval(id);
  }, [refresh]);

  const acceptancePie = data
    ? [
        { name: "Accepted", value: data.advisory.accepted, fill: "#22d3ee" },
        { name: "Rejected", value: data.advisory.rejected, fill: "#52525b" },
      ]
    : [];

  const fpPie = data
    ? [
        { name: "Successful", value: data.execution.total_executed - data.execution.false_positive, fill: "#4ade80" },
        { name: "False Positive", value: data.execution.false_positive, fill: "#f87171" },
      ]
    : [];

  return (
    <div className="flex h-screen bg-zinc-950 text-zinc-100 overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-auto p-6 space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">KPI Dashboard</h1>
            <p className="text-sm text-zinc-500 mt-0.5">
              Advisory quality & SRE performance — rolling 24h window
            </p>
          </div>
          <div className="flex items-center gap-3">
            {data?.source === "mock" && (
              <span className="text-xs text-amber-400 bg-amber-400/10 px-2 py-1 rounded-md border border-amber-400/20">
                Mock data
              </span>
            )}
            {lastRefresh && (
              <span className="text-xs text-zinc-500">
                <Clock className="inline h-3 w-3 mr-1" />
                {lastRefresh.toLocaleTimeString()}
              </span>
            )}
            <button
              onClick={refresh}
              disabled={loading}
              className="flex items-center gap-1.5 text-xs text-zinc-400 hover:text-zinc-100 transition-colors disabled:opacity-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
              Refresh
            </button>
          </div>
        </div>

        {/* KPI stat cards */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          {loading || !data ? (
            Array.from({ length: 4 }).map((_, i) => (
              <Card key={i} className="bg-zinc-900/60 border-zinc-800">
                <CardContent className="p-5">
                  <Skeleton className="h-16 w-full bg-zinc-800" />
                </CardContent>
              </Card>
            ))
          ) : (
            <>
              <StatCard
                title="Acceptance Rate"
                value={pct(data.advisory.acceptance_rate)}
                sub={`${data.advisory.accepted} / ${data.advisory.total} advisories`}
                icon={CheckCircle2}
                color="text-cyan-400"
              />
              <StatCard
                title="False Positive Rate"
                value={pct(data.execution.false_positive_rate)}
                sub={`${data.execution.false_positive} FP of ${data.execution.total_executed}`}
                icon={XCircle}
                color={
                  (data.execution.false_positive_rate ?? 0) > 0.15
                    ? "text-red-400"
                    : "text-emerald-400"
                }
              />
              <StatCard
                title="Total Advisories"
                value={data.advisory.total.toString()}
                sub={`${data.advisory.rejected} rejected`}
                icon={Activity}
                color="text-violet-400"
              />
              <StatCard
                title="Executed Actions"
                value={data.execution.total_executed.toString()}
                sub={`${data.execution.false_positive} needed rework`}
                icon={TrendingUp}
                color="text-amber-400"
              />
            </>
          )}
        </div>

        {/* Charts row */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Acceptance rate pie */}
          <Card className="bg-zinc-900/60 border-zinc-800">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-zinc-300">Advisory Outcomes</CardTitle>
            </CardHeader>
            <CardContent>
              {loading || !data ? (
                <Skeleton className="h-48 w-full bg-zinc-800" />
              ) : (
                <ResponsiveContainer width="100%" height={180}>
                  <PieChart>
                    <Pie
                      data={acceptancePie}
                      cx="50%"
                      cy="50%"
                      innerRadius={50}
                      outerRadius={75}
                      dataKey="value"
                      paddingAngle={2}
                    >
                      {acceptancePie.map((entry) => (
                        <Cell key={entry.name} fill={entry.fill} />
                      ))}
                    </Pie>
                    <Tooltip
                      contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", borderRadius: 6 }}
                      itemStyle={{ color: "#e4e4e7" }}
                    />
                    <Legend
                      formatter={(v) => <span className="text-xs text-zinc-400">{v}</span>}
                    />
                  </PieChart>
                </ResponsiveContainer>
              )}
            </CardContent>
          </Card>

          {/* False positive pie */}
          <Card className="bg-zinc-900/60 border-zinc-800">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-zinc-300">Execution Quality</CardTitle>
            </CardHeader>
            <CardContent>
              {loading || !data ? (
                <Skeleton className="h-48 w-full bg-zinc-800" />
              ) : (
                <ResponsiveContainer width="100%" height={180}>
                  <PieChart>
                    <Pie
                      data={fpPie}
                      cx="50%"
                      cy="50%"
                      innerRadius={50}
                      outerRadius={75}
                      dataKey="value"
                      paddingAngle={2}
                    >
                      {fpPie.map((entry) => (
                        <Cell key={entry.name} fill={entry.fill} />
                      ))}
                    </Pie>
                    <Tooltip
                      contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", borderRadius: 6 }}
                      itemStyle={{ color: "#e4e4e7" }}
                    />
                    <Legend
                      formatter={(v) => <span className="text-xs text-zinc-400">{v}</span>}
                    />
                  </PieChart>
                </ResponsiveContainer>
              )}
            </CardContent>
          </Card>

          {/* Lane breakdown bar chart */}
          <Card className="bg-zinc-900/60 border-zinc-800">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-zinc-300">Incidents by Lane</CardTitle>
            </CardHeader>
            <CardContent>
              {loading || !data ? (
                <Skeleton className="h-48 w-full bg-zinc-800" />
              ) : (
                <ResponsiveContainer width="100%" height={180}>
                  <BarChart data={data.trend} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
                    <XAxis
                      dataKey="lane"
                      tickFormatter={(v: string) => LANE_LABELS[v] ?? v}
                      tick={{ fontSize: 10, fill: "#71717a" }}
                    />
                    <YAxis tick={{ fontSize: 10, fill: "#71717a" }} />
                    <Tooltip
                      contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", borderRadius: 6 }}
                      itemStyle={{ color: "#e4e4e7" }}
                      labelFormatter={(v: string) => LANE_LABELS[v] ?? v}
                    />
                    <Bar dataKey="detected" name="Detected" radius={[2, 2, 0, 0]}>
                      {data.trend.map((entry) => (
                        <Cell key={entry.lane} fill={LANE_COLORS[entry.lane] ?? "#71717a"} />
                      ))}
                    </Bar>
                    <Bar dataKey="resolved" name="Resolved" fill="#4ade80" radius={[2, 2, 0, 0]} opacity={0.5} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Lane resolution table */}
        <Card className="bg-zinc-900/60 border-zinc-800">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-zinc-300">Lane Resolution Detail</CardTitle>
          </CardHeader>
          <CardContent>
            {loading || !data ? (
              <Skeleton className="h-32 w-full bg-zinc-800" />
            ) : (
              <div className="divide-y divide-zinc-800">
                {data.trend.map((row) => {
                  const resRate = row.detected > 0 ? row.resolved / row.detected : null;
                  const unresolved = row.detected - row.resolved;
                  return (
                    <div key={row.lane} className="flex items-center gap-4 py-3">
                      <div
                        className="h-2 w-2 rounded-full shrink-0"
                        style={{ background: LANE_COLORS[row.lane] ?? "#71717a" }}
                      />
                      <span className="text-sm text-zinc-300 w-28">
                        {LANE_LABELS[row.lane] ?? row.lane}
                      </span>
                      <div className="flex-1 flex gap-6 text-xs text-zinc-500">
                        <span>
                          <span className="text-zinc-200 font-medium">{row.detected}</span> detected
                        </span>
                        <span>
                          <span className="text-emerald-400 font-medium">{row.resolved}</span> resolved
                        </span>
                        {unresolved > 0 && (
                          <span>
                            <span className="text-amber-400 font-medium">{unresolved}</span> open
                          </span>
                        )}
                      </div>
                      <div className="w-32">
                        <div className="h-1.5 bg-zinc-800 rounded-full overflow-hidden">
                          <div
                            className="h-full rounded-full"
                            style={{
                              width: `${(resRate ?? 0) * 100}%`,
                              background: LANE_COLORS[row.lane] ?? "#71717a",
                            }}
                          />
                        </div>
                      </div>
                      <span className="text-xs text-zinc-400 w-12 text-right">
                        {resRate != null ? `${(resRate * 100).toFixed(0)}%` : "—"}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </main>
    </div>
  );
}
