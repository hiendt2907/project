"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Cpu, AlertTriangle, Globe, ShieldAlert } from "lucide-react";

export interface DiagnosticLanesData {
  sys_resource: {
    status: "ok" | "warn" | "critical";
    z_cpu: number;
    z_mem: number;
    baseline_age_sec: number;
    anomaly_triggered: boolean;
  };
  sys_hard_fail: {
    status: "ok" | "warn" | "critical";
    crash_loops: number;
    broken_specs: number;
    advisory_count_24h: number;
  };
  app_http: {
    status: "ok" | "warn" | "critical";
    surge_triggered: boolean;
    dominant_error_class: "5xx" | "429" | "499" | "401" | "403" | "none";
    error_rate_pct: number;
    sigma_bypass: boolean;
  };
  siem_security: {
    status: "ok" | "warn" | "critical";
    active_incidents: number;
    kill_chain_stage: string;
    pipeline_healthy: boolean;
    forecast_1h_severity: string;
  };
}

interface DiagnosticLanesProps {
  data: DiagnosticLanesData | null;
}

const statusBadge: Record<"ok" | "warn" | "critical", string> = {
  ok: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  warn: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  critical: "bg-rose-500/15 text-rose-400 border-rose-500/30",
};

const statusDot: Record<"ok" | "warn" | "critical", string> = {
  ok: "bg-emerald-400",
  warn: "bg-amber-400",
  critical: "bg-rose-400",
};

function StatusBadge({ status }: { status: "ok" | "warn" | "critical" }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[9px] uppercase tracking-wide font-medium ${statusBadge[status]}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${statusDot[status]}`} />
      {status}
    </span>
  );
}

function MetaRow({ label, value }: { label: string; value: string | number | boolean }) {
  const display = typeof value === "boolean" ? (value ? "YES" : "NO") : value;
  return (
    <div className="flex items-center justify-between text-[11px]">
      <span className="text-zinc-500">{label}</span>
      <span className="font-mono text-zinc-300">{String(display)}</span>
    </div>
  );
}

export function DiagnosticLanes({ data }: DiagnosticLanesProps) {
  if (data === null) {
    return (
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <Card key={i} className="border-zinc-800 bg-zinc-900/60">
            <CardContent className="p-4">
              <Skeleton className="h-28 w-full bg-zinc-800" />
            </CardContent>
          </Card>
        ))}
      </div>
    );
  }

  const lanes = [
    {
      id: "sys_resource",
      title: "L1 · SYS_RESOURCE",
      icon: Cpu,
      status: data.sys_resource.status,
      rows: [
        { label: "z-CPU", value: `${data.sys_resource.z_cpu}σ` },
        { label: "z-MEM", value: `${data.sys_resource.z_mem}σ` },
        { label: "Baseline", value: `${data.sys_resource.baseline_age_sec}s ago` },
        { label: "Anomaly", value: data.sys_resource.anomaly_triggered },
      ],
    },
    {
      id: "sys_hard_fail",
      title: "L2 · SYS_HARD_FAIL",
      icon: AlertTriangle,
      status: data.sys_hard_fail.status,
      rows: [
        { label: "Crash loops", value: data.sys_hard_fail.crash_loops },
        { label: "Broken specs", value: data.sys_hard_fail.broken_specs },
        { label: "Advisories 24h", value: data.sys_hard_fail.advisory_count_24h },
      ],
    },
    {
      id: "app_http",
      title: "L3 · APP_HTTP",
      icon: Globe,
      status: data.app_http.status,
      rows: [
        { label: "Error rate", value: `${data.app_http.error_rate_pct}%` },
        { label: "Class", value: data.app_http.dominant_error_class },
        { label: "Σ-bypass", value: data.app_http.sigma_bypass },
        { label: "Surge", value: data.app_http.surge_triggered },
      ],
    },
    {
      id: "siem_security",
      title: "L4 · SIEM_SECURITY",
      icon: ShieldAlert,
      status: data.siem_security.status,
      rows: [
        { label: "Active incidents", value: data.siem_security.active_incidents },
        { label: "Kill-chain", value: data.siem_security.kill_chain_stage },
        { label: "Pipeline", value: data.siem_security.pipeline_healthy ? "healthy" : "unhealthy" },
        { label: "+1h forecast", value: data.siem_security.forecast_1h_severity },
      ],
    },
  ] as const;

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      {lanes.map(({ id, title, icon: Icon, status, rows }) => (
        <Card key={id} className="border-zinc-800 bg-zinc-900/60">
          <CardHeader className="pb-2 pt-4 px-4">
            <div className="flex items-center justify-between">
              <CardTitle className="flex items-center gap-2 text-sm font-medium text-zinc-300">
                <Icon className="h-3.5 w-3.5 text-zinc-500" />
                {title}
              </CardTitle>
              <StatusBadge status={status} />
            </div>
          </CardHeader>
          <CardContent className="space-y-1.5 px-4 pb-4">
            {rows.map((row) => (
              <MetaRow key={row.label} label={row.label} value={row.value} />
            ))}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
