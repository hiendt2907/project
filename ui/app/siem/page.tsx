"use client";

import { useEffect, useState, useCallback } from "react";
import { Sidebar } from "@/components/sidebar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  PieChart,
  Pie,
  Cell,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts";
import { Shield, RefreshCw, Clock, AlertTriangle } from "lucide-react";
import type { SiemOpsResponse, SiemCategoryDistribution, SiemRecentIncident } from "@/app/api/siem-incidents/route";

const CATEGORY_COLORS: Record<keyof SiemCategoryDistribution, string> = {
  ddos: "#ef4444",
  malware: "#f97316",
  data_exfil: "#eab308",
  k8s_threat: "#a855f7",
  auth_failure: "#3b82f6",
  lateral_movement: "#ec4899",
  network_anomaly: "#22d3ee",
};

const CATEGORY_LABELS: Record<keyof SiemCategoryDistribution, string> = {
  ddos: "DDoS",
  malware: "Malware",
  data_exfil: "Data Exfil",
  k8s_threat: "K8s Threat",
  auth_failure: "Auth Failure",
  lateral_movement: "Lateral Movement",
  network_anomaly: "Network Anomaly",
};

const KILL_CHAIN_STAGES = [
  { key: "initial_access", label: "Initial Access" },
  { key: "execution", label: "Execution" },
  { key: "persistence", label: "Persistence" },
  { key: "lateral_movement", label: "Lateral Movement" },
  { key: "exfiltration", label: "Exfiltration" },
];

const STATUS_BADGE: Record<SiemRecentIncident["status"], string> = {
  ACTIVE: "bg-blue-500/15 text-blue-400 border-blue-500/30",
  HITL_PENDING: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  RESOLVED: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
};

const SEV_COLOR: Record<SiemRecentIncident["severity"], string> = {
  critical: "text-rose-400",
  high: "text-orange-400",
  medium: "text-amber-400",
  low: "text-sky-400",
};

const tooltipStyle = {
  background: "#09090b",
  border: "1px solid #27272a",
  borderRadius: 8,
  fontSize: 12,
  color: "#e4e4e7",
};

interface StatCardProps {
  label: string;
  value: number;
  color: string;
  sub?: string;
}

function StatCard({ label, value, color, sub }: StatCardProps) {
  return (
    <Card className="border-zinc-800 bg-zinc-900/60">
      <CardContent className="p-5">
        <p className="text-[10px] uppercase tracking-widest text-zinc-500">{label}</p>
        <p className={`mt-1 font-mono text-2xl font-bold ${color}`}>{value}</p>
        {sub && <p className="mt-0.5 text-[11px] text-zinc-600">{sub}</p>}
      </CardContent>
    </Card>
  );
}

export default function SiemOpsPage() {
  const [data, setData] = useState<SiemOpsResponse | null>(null);
  const [lastRefresh, setLastRefresh] = useState(new Date());

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/siem-incidents", { cache: "no-store" });
      if (res.ok) {
        setData(await res.json());
        setLastRefresh(new Date());
      }
    } catch {
      // keep existing data
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 15_000);
    return () => clearInterval(t);
  }, [load]);

  const pieData = data
    ? Object.entries(data.category_distribution).map(([key, value]) => ({
        name: CATEGORY_LABELS[key as keyof SiemCategoryDistribution] ?? key,
        value,
        key,
      }))
    : [];

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        <header className="sticky top-0 z-10 flex h-16 items-center justify-between border-b border-zinc-800 bg-zinc-950/80 px-6 backdrop-blur">
          <div className="flex items-center gap-3">
            <h1 className="text-base font-semibold text-zinc-100">SIEM Operations</h1>
            <span className="flex items-center gap-1.5 rounded-full bg-emerald-500/10 border border-emerald-500/20 px-2.5 py-0.5 text-[10px] font-medium text-emerald-400">
              <span className="relative flex h-1.5 w-1.5">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-400" />
              </span>
              Live
            </span>
          </div>
          <div className="flex items-center gap-3 text-xs text-zinc-600">
            <span className="flex items-center gap-1">
              <span className={`h-1.5 w-1.5 rounded-full ${data?.source === "gateway" ? "bg-emerald-400" : "bg-amber-400"}`} />
              {data?.source === "gateway" ? "live data" : "mock fallback"}
            </span>
            <span className="flex items-center gap-1">
              <RefreshCw className="h-3 w-3" />
              {lastRefresh.toLocaleTimeString()}
            </span>
          </div>
        </header>

        <div className="p-6 space-y-6">
          {/* Stat cards */}
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            {data ? (
              <>
                <StatCard label="Incidents · 24h" value={data.total_24h} color="text-cyan-400" sub="ingested" />
                <StatCard label="Critical" value={data.critical_count} color="text-rose-400" sub="severity=critical" />
                <StatCard
                  label="HITL Pending"
                  value={data.hitl_pending}
                  color="text-amber-400"
                  sub="awaiting approval"
                />
                <StatCard label="Resolved · 24h" value={data.resolved_24h} color="text-emerald-400" sub="closed" />
              </>
            ) : (
              Array.from({ length: 4 }).map((_, i) => (
                <Card key={i} className="border-zinc-800 bg-zinc-900/60">
                  <CardContent className="p-5">
                    <Skeleton className="h-14 w-full bg-zinc-800" />
                  </CardContent>
                </Card>
              ))
            )}
          </div>

          {/* Charts row */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
            {/* Category distribution donut */}
            <Card className="border-zinc-800 bg-zinc-900/60 lg:col-span-2">
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-zinc-300">
                  <Shield className="mr-2 inline h-3.5 w-3.5 text-zinc-500" />
                  Category Distribution
                </CardTitle>
              </CardHeader>
              <CardContent>
                {data ? (
                  <ResponsiveContainer width="100%" height={240}>
                    <PieChart>
                      <Pie
                        data={pieData}
                        cx="50%"
                        cy="50%"
                        innerRadius={60}
                        outerRadius={90}
                        dataKey="value"
                        stroke="#18181b"
                        strokeWidth={2}
                      >
                        {pieData.map((entry) => (
                          <Cell
                            key={entry.key}
                            fill={CATEGORY_COLORS[entry.key as keyof SiemCategoryDistribution] ?? "#71717a"}
                          />
                        ))}
                      </Pie>
                      <Tooltip contentStyle={tooltipStyle} />
                      <Legend
                        wrapperStyle={{ fontSize: 10, color: "#a1a1aa" }}
                        formatter={(v) => v}
                      />
                    </PieChart>
                  </ResponsiveContainer>
                ) : (
                  <Skeleton className="h-60 w-full bg-zinc-800" />
                )}
              </CardContent>
            </Card>

            {/* Kill-chain timeline */}
            <Card className="border-zinc-800 bg-zinc-900/60 lg:col-span-3">
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-zinc-300">Kill-Chain Progression</CardTitle>
              </CardHeader>
              <CardContent>
                {data ? (
                  <div className="relative">
                    <div className="absolute left-4 top-3 bottom-3 w-px bg-zinc-800" />
                    <div className="space-y-4 pl-10">
                      {KILL_CHAIN_STAGES.map((stage, i) => {
                        const active = data.kill_chain_active_stages.includes(stage.key);
                        return (
                          <div key={stage.key} className="relative flex items-center gap-4">
                            <div
                              className={`absolute -left-[26px] flex h-4 w-4 items-center justify-center rounded-full border-2 ${
                                active
                                  ? "border-rose-500 bg-rose-500/20"
                                  : "border-zinc-700 bg-zinc-900"
                              }`}
                            >
                              {active && (
                                <span className="h-1.5 w-1.5 rounded-full bg-rose-400" />
                              )}
                            </div>
                            <div className="flex flex-1 items-center justify-between rounded-lg border border-zinc-800/60 bg-zinc-950/40 px-3 py-2.5">
                              <div className="flex items-center gap-3">
                                <span className="font-mono text-[10px] text-zinc-600">
                                  {String(i + 1).padStart(2, "0")}
                                </span>
                                <span className={`text-sm font-medium ${active ? "text-zinc-100" : "text-zinc-500"}`}>
                                  {stage.label}
                                </span>
                              </div>
                              {active ? (
                                <span className="inline-flex items-center gap-1 rounded border border-rose-500/30 bg-rose-500/10 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-rose-400">
                                  <span className="relative flex h-1.5 w-1.5">
                                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-rose-400 opacity-75" />
                                    <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-rose-400" />
                                  </span>
                                  Active
                                </span>
                              ) : (
                                <span className="text-[9px] uppercase tracking-wide text-zinc-700">Inactive</span>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                ) : (
                  <Skeleton className="h-60 w-full bg-zinc-800" />
                )}
              </CardContent>
            </Card>
          </div>

          {/* Recent SIEM incidents table */}
          <Card className="border-zinc-800 bg-zinc-900/60">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-zinc-300">Recent SIEM Incidents</CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-zinc-800 text-left text-[10px] uppercase tracking-widest text-zinc-500">
                      <th className="px-4 py-3">Time</th>
                      <th className="px-4 py-3">Category</th>
                      <th className="px-4 py-3">Severity</th>
                      <th className="px-4 py-3">Tenant</th>
                      <th className="px-4 py-3">Source IP</th>
                      <th className="px-4 py-3">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-800/60">
                    {!data ? (
                      Array.from({ length: 5 }).map((_, i) => (
                        <tr key={i}>
                          <td colSpan={6} className="px-4 py-3">
                            <Skeleton className="h-4 w-full bg-zinc-800" />
                          </td>
                        </tr>
                      ))
                    ) : data.recent_incidents.length === 0 ? (
                      <tr>
                        <td colSpan={6} className="px-4 py-10 text-center text-zinc-600">
                          <AlertTriangle className="mx-auto mb-2 h-6 w-6 opacity-30" />
                          No recent incidents
                        </td>
                      </tr>
                    ) : (
                      data.recent_incidents.map((inc) => (
                        <tr key={inc.id} className="hover:bg-zinc-800/30 transition-colors">
                          <td className="px-4 py-3 font-mono text-zinc-500">
                            <div className="flex items-center gap-1">
                              <Clock className="h-3 w-3" />
                              {new Date(inc.timestamp).toLocaleTimeString()}
                            </div>
                          </td>
                          <td className="px-4 py-3">
                            <span
                              className="inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium"
                              style={{
                                color: CATEGORY_COLORS[inc.category] ?? "#71717a",
                                borderColor: `${CATEGORY_COLORS[inc.category] ?? "#71717a"}44`,
                                backgroundColor: `${CATEGORY_COLORS[inc.category] ?? "#71717a"}15`,
                              }}
                            >
                              {CATEGORY_LABELS[inc.category] ?? inc.category}
                            </span>
                          </td>
                          <td className={`px-4 py-3 font-mono font-semibold uppercase text-[10px] ${SEV_COLOR[inc.severity]}`}>
                            {inc.severity}
                          </td>
                          <td className="px-4 py-3 font-mono text-zinc-400">{inc.tenant}</td>
                          <td className="px-4 py-3 font-mono text-zinc-500">{inc.source_ip}</td>
                          <td className="px-4 py-3">
                            <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${STATUS_BADGE[inc.status]}`}>
                              {inc.status.replace("_", " ")}
                            </span>
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        </div>
      </main>
    </div>
  );
}
