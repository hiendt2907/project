"use client";

import { useCallback, useEffect, useState } from "react";
import { Sidebar } from "@/components/sidebar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Rocket,
  RefreshCw,
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Activity,
  Server,
  RotateCcw,
  ChevronDown,
  ChevronRight,
  Wifi,
  Database,
  Cpu,
  MessageSquare,
  BarChart3,
} from "lucide-react";

type ComponentStatus = "running" | "degraded" | "down";

interface DeployComponent {
  name: string;
  role: string;
  current_version: string;
  status: ComponentStatus;
  last_deployed: string;
  replicas: number;
}

interface DeployData {
  components: DeployComponent[];
  source: string;
  note?: string;
}

interface ConnCheck {
  label: string;
  key: string;
  icon: typeof Wifi;
  status: "ok" | "error" | "checking";
  detail?: string;
}

const ACTIVITY_LOG = [
  { time: "2026-05-11T00:00:00Z", event: "Rollout complete", detail: "v2.3.1-sprint5 deployed to all components", ok: true },
  { time: "2026-05-10T22:30:00Z", event: "Rollout started", detail: "Triggered by CI pipeline", ok: true },
  { time: "2026-05-10T20:15:00Z", event: "Health check pass", detail: "All readinessProbes green", ok: true },
  { time: "2026-05-10T18:00:00Z", event: "Image pushed", detail: "omni-worker:v2.3.1-sprint5", ok: true },
  { time: "2026-05-09T14:00:00Z", event: "Rollback triggered", detail: "v2.3.0 restored — analyst OOM", ok: false },
];

const statusConfig: Record<ComponentStatus, { icon: typeof CheckCircle2; color: string; bg: string; label: string }> = {
  running: { icon: CheckCircle2, color: "text-emerald-400", bg: "bg-emerald-500/10 border-emerald-900/40", label: "Running" },
  degraded: { icon: AlertTriangle, color: "text-amber-400", bg: "bg-amber-500/10 border-amber-900/40", label: "Degraded" },
  down: { icon: XCircle, color: "text-red-400", bg: "bg-red-500/10 border-red-900/40", label: "Down" },
};

const CONN_CHECKS_DEFAULT: ConnCheck[] = [
  { label: "Gateway API", key: "gateway", icon: Server, status: "checking" },
  { label: "Redis", key: "redis", icon: Database, status: "checking" },
  { label: "Kafka", key: "kafka", icon: MessageSquare, status: "checking" },
  { label: "Ollama LLM", key: "ollama", icon: Cpu, status: "checking" },
  { label: "Prometheus", key: "prometheus", icon: BarChart3, status: "checking" },
];

function fmt(iso: string) {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function DeployPage() {
  const [data, setData] = useState<DeployData | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState(new Date());
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmTarget, setConfirmTarget] = useState<string>("all");
  const [restarting, setRestarting] = useState<Set<string>>(new Set());
  const [connOpen, setConnOpen] = useState(false);
  const [connChecks, setConnChecks] = useState<ConnCheck[]>(CONN_CHECKS_DEFAULT);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/deploy", { cache: "no-store" });
      if (res.ok) {
        setData(await res.json());
        setLastRefresh(new Date());
      }
    } catch {
      // keep existing
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [load]);

  async function triggerRollout(target: string) {
    setConfirmOpen(false);
    const affected = target === "all"
      ? (data?.components ?? []).map((c) => c.name)
      : [target];

    setRestarting(new Set(affected));

    await fetch("/api/deploy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target }),
    });

    // Simulate rolling restart delay (3s per component)
    setTimeout(() => {
      setRestarting(new Set());
      load();
    }, affected.length * 800 + 3000);
  }

  async function runConnChecks() {
    setConnChecks(CONN_CHECKS_DEFAULT.map((c) => ({ ...c, status: "checking" })));
    // Mock all green after a brief delay
    setTimeout(() => {
      setConnChecks(
        CONN_CHECKS_DEFAULT.map((c) => ({
          ...c,
          status: "ok",
          detail: "200 OK",
        }))
      );
    }, 1200);
  }

  const components = data?.components ?? [];
  const runningCount = components.filter((c) => c.status === "running").length;
  const degradedCount = components.filter((c) => c.status === "degraded").length;
  const downCount = components.filter((c) => c.status === "down").length;

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        {/* Header */}
        <header className="sticky top-0 z-10 flex h-16 items-center justify-between border-b border-zinc-800 bg-zinc-950/80 px-6 backdrop-blur">
          <div>
            <h1 className="text-base font-semibold text-zinc-100">Deployment Center</h1>
            <p className="text-xs text-zinc-500">
              Last deploy:{" "}
              {components[0]?.last_deployed ? fmt(components[0].last_deployed) : "—"}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={load}
              className="flex items-center gap-1.5 rounded border border-zinc-700 px-2.5 py-1.5 text-xs text-zinc-400 hover:border-zinc-600 hover:text-zinc-100 transition-colors"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              Refresh
            </button>
            <button
              onClick={() => { setConfirmTarget("all"); setConfirmOpen(true); }}
              className="flex items-center gap-1.5 rounded border border-cyan-600 bg-cyan-500/10 px-3 py-1.5 text-xs text-cyan-400 hover:bg-cyan-500/20 transition-colors"
            >
              <Rocket className="h-3.5 w-3.5" />
              Rollout All
            </button>
          </div>
        </header>

        <div className="p-6 space-y-6">
          {/* Status summary */}
          <div className="grid grid-cols-3 gap-3">
            {[
              { label: "Running", count: runningCount, color: "text-emerald-400" },
              { label: "Degraded", count: degradedCount, color: "text-amber-400" },
              { label: "Down", count: downCount, color: "text-red-400" },
            ].map(({ label, count, color }) => (
              <Card key={label} className="border-zinc-800 bg-zinc-900/60">
                <CardContent className="flex items-center gap-3 p-4">
                  <div>
                    <p className="text-[10px] uppercase tracking-widest text-zinc-500">{label}</p>
                    <p className={`text-2xl font-bold font-mono ${color}`}>{count}</p>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>

          {/* Component grid */}
          <div>
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-widest text-zinc-500">Components</h2>
            {loading ? (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {Array.from({ length: 7 }).map((_, i) => (
                  <Skeleton key={i} className="h-32 bg-zinc-900" />
                ))}
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {components.map((comp) => {
                  const cfg = statusConfig[comp.status];
                  const Icon = cfg.icon;
                  const isRestarting = restarting.has(comp.name);
                  return (
                    <Card key={comp.name} className={`border ${cfg.bg}`}>
                      <CardHeader className="p-4 pb-2">
                        <div className="flex items-center justify-between">
                          <CardTitle className="flex items-center gap-2 text-sm text-zinc-100">
                            <Server className="h-4 w-4 text-zinc-500" />
                            {comp.name}
                          </CardTitle>
                          <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium ${cfg.bg} ${cfg.color}`}>
                            <Icon className="h-3 w-3" />
                            {isRestarting ? "Restarting…" : cfg.label}
                          </span>
                        </div>
                      </CardHeader>
                      <CardContent className="p-4 pt-0 space-y-3">
                        <div className="flex items-center justify-between text-xs">
                          <span className="text-zinc-500">Version</span>
                          <span className="font-mono text-cyan-400">{comp.current_version}</span>
                        </div>
                        <div className="flex items-center justify-between text-xs">
                          <span className="text-zinc-500">Replicas</span>
                          <span className="font-mono text-zinc-300">{comp.replicas}</span>
                        </div>
                        <div className="flex items-center justify-between text-xs">
                          <span className="text-zinc-500">Last deployed</span>
                          <span className="text-zinc-400">{fmt(comp.last_deployed).split(",")[0]}</span>
                        </div>
                        <button
                          disabled={isRestarting}
                          onClick={() => { setConfirmTarget(comp.name); setConfirmOpen(true); }}
                          className="flex w-full items-center justify-center gap-1.5 rounded border border-zinc-700 py-1.5 text-[11px] text-zinc-400 hover:border-zinc-500 hover:text-zinc-200 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                        >
                          <RotateCcw className="h-3 w-3" />
                          {isRestarting ? "Restarting…" : "Restart"}
                        </button>
                      </CardContent>
                    </Card>
                  );
                })}
              </div>
            )}
          </div>

          {/* Connection tester */}
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/40">
            <button
              onClick={() => setConnOpen((v) => !v)}
              className="flex w-full items-center justify-between px-4 py-3 text-sm text-zinc-300 hover:bg-zinc-800/40 transition-colors"
            >
              <span className="flex items-center gap-2">
                <Activity className="h-4 w-4 text-zinc-500" />
                Connectivity Tester
              </span>
              {connOpen ? <ChevronDown className="h-4 w-4 text-zinc-500" /> : <ChevronRight className="h-4 w-4 text-zinc-500" />}
            </button>
            {connOpen && (
              <div className="border-t border-zinc-800 p-4 space-y-3">
                <div className="space-y-2">
                  {connChecks.map((check) => {
                    const Icon = check.icon;
                    return (
                      <div key={check.key} className="flex items-center justify-between rounded border border-zinc-800 bg-zinc-900 px-3 py-2">
                        <span className="flex items-center gap-2 text-xs text-zinc-300">
                          <Icon className="h-3.5 w-3.5 text-zinc-500" />
                          {check.label}
                        </span>
                        <span className={`text-[11px] font-mono ${check.status === "ok" ? "text-emerald-400" : check.status === "error" ? "text-red-400" : "text-zinc-500"}`}>
                          {check.status === "checking" ? "—" : check.status === "ok" ? "200 OK" : check.detail ?? "Error"}
                        </span>
                      </div>
                    );
                  })}
                </div>
                <button
                  onClick={runConnChecks}
                  className="flex items-center gap-1.5 rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-400 hover:border-zinc-500 hover:text-zinc-100 transition-colors"
                >
                  <Activity className="h-3.5 w-3.5" />
                  Test All
                </button>
              </div>
            )}
          </div>

          {/* Activity log */}
          <div>
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-widest text-zinc-500">Activity Log</h2>
            <div className="space-y-1.5">
              {ACTIVITY_LOG.map((entry, i) => (
                <div key={i} className="flex items-start gap-3 rounded-md border border-zinc-800 bg-zinc-900/40 px-3 py-2.5">
                  <div className={`mt-0.5 h-2 w-2 rounded-full shrink-0 ${entry.ok ? "bg-emerald-500" : "bg-red-500"}`} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-medium text-zinc-200">{entry.event}</span>
                      <span className="text-[10px] font-mono text-zinc-600 shrink-0">{fmt(entry.time).split(",")[0]}</span>
                    </div>
                    <p className="text-[11px] text-zinc-500 mt-0.5">{entry.detail}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Confirm dialog */}
        {confirmOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
            <div className="w-full max-w-md rounded-xl border border-zinc-700 bg-zinc-900 p-6 shadow-2xl">
              <h3 className="text-base font-semibold text-zinc-100">Confirm Rollout</h3>
              <p className="mt-2 text-sm text-zinc-400">
                {confirmTarget === "all"
                  ? "This will restart all components. Continue?"
                  : `Restart ${confirmTarget}? This will briefly interrupt the service.`}
              </p>
              <div className="mt-5 flex justify-end gap-3">
                <button
                  onClick={() => setConfirmOpen(false)}
                  className="rounded border border-zinc-700 px-4 py-2 text-sm text-zinc-400 hover:border-zinc-500 hover:text-zinc-200 transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={() => triggerRollout(confirmTarget)}
                  className="rounded bg-cyan-600 px-4 py-2 text-sm font-medium text-white hover:bg-cyan-500 transition-colors"
                >
                  Confirm
                </button>
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
