"use client";

import { useEffect, useState, useCallback } from "react";
import { Sidebar } from "@/components/sidebar";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Server,
  CheckCircle2,
  AlertTriangle,
  XCircle,
  RefreshCw,
  Clock,
  ChevronDown,
  ChevronUp,
} from "lucide-react";
import type { WorkerDetail, WorkersResponse, WorkerStatus } from "@/app/api/workers/route";

const STATUS_BORDER: Record<WorkerStatus, string> = {
  healthy: "border-emerald-500/40",
  degraded: "border-amber-500/40",
  unhealthy: "border-rose-500/40",
  unknown: "border-zinc-700",
};

const STATUS_BG: Record<WorkerStatus, string> = {
  healthy: "bg-emerald-500/5",
  degraded: "bg-amber-500/5",
  unhealthy: "bg-rose-500/5",
  unknown: "bg-zinc-900/60",
};

const STATUS_LABEL: Record<WorkerStatus, string> = {
  healthy: "Healthy",
  degraded: "Degraded",
  unhealthy: "Unhealthy",
  unknown: "Unknown",
};

const STATUS_COLOR: Record<WorkerStatus, string> = {
  healthy: "text-emerald-400",
  degraded: "text-amber-400",
  unhealthy: "text-rose-400",
  unknown: "text-zinc-500",
};

const STATUS_ICON: Record<WorkerStatus, typeof CheckCircle2> = {
  healthy: CheckCircle2,
  degraded: AlertTriangle,
  unhealthy: XCircle,
  unknown: AlertTriangle,
};

function heartbeatLabel(seconds: number): string {
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function WorkerCard({ worker }: { worker: WorkerDetail }) {
  const [expanded, setExpanded] = useState(false);
  const Icon = STATUS_ICON[worker.status];

  return (
    <div
      className={`rounded-lg border p-4 transition-colors ${STATUS_BORDER[worker.status]} ${STATUS_BG[worker.status]}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-zinc-800">
            <Server className={`h-4 w-4 ${STATUS_COLOR[worker.status]}`} />
          </div>
          <div className="min-w-0">
            <p className="font-mono text-sm font-semibold text-zinc-100">{worker.role}</p>
            <div className="flex items-center gap-1.5 mt-0.5">
              <Icon className={`h-3 w-3 ${STATUS_COLOR[worker.status]}`} />
              <span className={`text-[11px] font-medium ${STATUS_COLOR[worker.status]}`}>
                {STATUS_LABEL[worker.status]}
              </span>
            </div>
          </div>
        </div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="shrink-0 text-zinc-600 hover:text-zinc-400 transition-colors"
        >
          {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </button>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2">
        <div className="rounded bg-zinc-950/50 px-2 py-1.5">
          <p className="text-[9px] uppercase tracking-wider text-zinc-600">Replicas</p>
          <p className="font-mono text-sm font-bold text-zinc-200">
            {worker.ready}
            <span className="text-zinc-600">/{worker.replicas}</span>
          </p>
        </div>
        <div className="rounded bg-zinc-950/50 px-2 py-1.5">
          <p className="text-[9px] uppercase tracking-wider text-zinc-600">Last Seen</p>
          <div className="flex items-center gap-1 mt-0.5">
            <Clock className="h-3 w-3 text-zinc-600" />
            <p className={`text-[11px] font-mono ${worker.last_heartbeat_age_seconds > 90 ? "text-amber-400" : "text-zinc-300"}`}>
              {heartbeatLabel(worker.last_heartbeat_age_seconds)}
            </p>
          </div>
        </div>
        <div className="rounded bg-zinc-950/50 px-2 py-1.5">
          <p className="text-[9px] uppercase tracking-wider text-zinc-600">Errors 24h</p>
          <p className={`font-mono text-sm font-bold ${worker.error_count_24h > 0 ? "text-amber-400" : "text-zinc-600"}`}>
            {worker.error_count_24h}
          </p>
        </div>
      </div>

      {expanded && (
        <div className="mt-3 space-y-2 border-t border-zinc-800/60 pt-3">
          <div>
            <p className="text-[10px] uppercase tracking-wider text-zinc-600 mb-1">Description</p>
            <p className="text-[11px] text-zinc-400">{worker.description}</p>
          </div>
          <div>
            <p className="text-[10px] uppercase tracking-wider text-zinc-600 mb-1">Last Message Type</p>
            <code className="text-[11px] font-mono text-zinc-400 bg-zinc-950/60 px-1.5 py-0.5 rounded">
              {worker.last_message_type}
            </code>
          </div>
        </div>
      )}
    </div>
  );
}

export default function WorkersPage() {
  const [data, setData] = useState<WorkersResponse | null>(null);
  const [lastRefresh, setLastRefresh] = useState(new Date());

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/workers", { cache: "no-store" });
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
    const t = setInterval(load, 10_000);
    return () => clearInterval(t);
  }, [load]);

  const overallColor: Record<WorkerStatus, string> = {
    healthy: "text-emerald-400",
    degraded: "text-amber-400",
    unhealthy: "text-rose-400",
    unknown: "text-zinc-500",
  };

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        <header className="sticky top-0 z-10 flex h-16 items-center justify-between border-b border-zinc-800 bg-zinc-950/80 px-6 backdrop-blur">
          <div className="flex items-center gap-3">
            <h1 className="text-base font-semibold text-zinc-100">Worker Fleet</h1>
            {data && (
              <span className={`text-xs font-semibold ${overallColor[data.overall]}`}>
                {STATUS_LABEL[data.overall]}
              </span>
            )}
          </div>
          <div className="flex items-center gap-3 text-xs text-zinc-600">
            <span className="flex items-center gap-1">
              <span className={`h-1.5 w-1.5 rounded-full ${data?.source === "gateway" ? "bg-emerald-400" : "bg-amber-400"}`} />
              {data?.source === "gateway" ? "live" : "mock"}
            </span>
            <span className="flex items-center gap-1">
              <RefreshCw className="h-3 w-3" />
              {lastRefresh.toLocaleTimeString()}
            </span>
          </div>
        </header>

        <div className="p-6 space-y-6">
          {/* Summary bar */}
          {data ? (
            <div className="flex items-center gap-4">
              <div className="flex items-center gap-2 rounded-lg border border-emerald-500/20 bg-emerald-500/5 px-4 py-2">
                <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                <span className="text-sm font-semibold text-emerald-400">{data.healthy_count}</span>
                <span className="text-xs text-zinc-500">healthy</span>
              </div>
              <div className="flex items-center gap-2 rounded-lg border border-amber-500/20 bg-amber-500/5 px-4 py-2">
                <AlertTriangle className="h-4 w-4 text-amber-400" />
                <span className="text-sm font-semibold text-amber-400">{data.degraded_count}</span>
                <span className="text-xs text-zinc-500">degraded</span>
              </div>
              <div className="flex items-center gap-2 rounded-lg border border-rose-500/20 bg-rose-500/5 px-4 py-2">
                <XCircle className="h-4 w-4 text-rose-400" />
                <span className="text-sm font-semibold text-rose-400">{data.unhealthy_count}</span>
                <span className="text-xs text-zinc-500">unhealthy</span>
              </div>
              <span className="ml-auto text-xs text-zinc-600">
                {data.workers.length} workers · refreshes every 10s
              </span>
            </div>
          ) : (
            <div className="flex gap-4">
              {Array.from({ length: 3 }).map((_, i) => (
                <Card key={i} className="border-zinc-800 bg-zinc-900/60 w-32">
                  <CardContent className="p-3">
                    <Skeleton className="h-8 w-full bg-zinc-800" />
                  </CardContent>
                </Card>
              ))}
            </div>
          )}

          {/* Worker grid */}
          {!data ? (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {Array.from({ length: 7 }).map((_, i) => (
                <div key={i} className="rounded-lg border border-zinc-800 p-4">
                  <Skeleton className="h-28 w-full bg-zinc-800" />
                </div>
              ))}
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {data.workers.map((w) => (
                <WorkerCard key={w.role} worker={w} />
              ))}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
