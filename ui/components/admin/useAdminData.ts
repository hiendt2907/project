"use client";

// Container hook for the admin dashboard — owns all data fetching and polling.
// Presentational panels receive plain props.

import { useEffect, useState } from "react";
import type { WorkersResponse } from "@/app/api/workers/route";
import type { CratResponse } from "@/app/api/crat/route";
import type { RemoteAgentsResponse } from "@/app/api/remote-agents/route";
import type { AutonomyPolicyResponse } from "@/app/api/config/autonomy/route";
import type { PodInfo, DeployEntry, KpiSummary, SiemTelemetry } from "./types";

function workersToPodsMap(data: WorkersResponse): PodInfo[] {
  return data.workers.map((w) => ({
    name: w.role,
    status: w.status === "unknown" ? "degraded" : w.status,
    ready: `${w.ready}/${w.replicas}`,
    hb: `${w.last_heartbeat_age_seconds}s`,
    error_count: (w as { error_count_24h?: number }).error_count_24h,
  }));
}

export interface AdminData {
  pods: PodInfo[] | null;
  crat: CratResponse | null;
  autonomy: AutonomyPolicyResponse | null;
  remoteAgents: RemoteAgentsResponse | null;
  deploy: DeployEntry[] | null;
  kpi: KpiSummary | null;
  siem: SiemTelemetry | null;
}

export type AdminErrorKey = "workers" | "crat" | "autonomy" | "remoteAgents" | "deploy" | "kpi" | "siem";
export type AdminErrors = Record<AdminErrorKey, boolean>;

function isErr(v: unknown): boolean {
  return (v as { source?: string } | null)?.source === "error";
}

export function useAdminData(tenant: string): AdminData & { errors: AdminErrors; reloadAutonomy: () => void } {
  const [pods, setPods] = useState<PodInfo[] | null>(null);
  const [crat, setCrat] = useState<CratResponse | null>(null);
  const [autonomy, setAutonomy] = useState<AutonomyPolicyResponse | null>(null);
  const [remoteAgents, setRemoteAgents] = useState<RemoteAgentsResponse | null>(null);
  const [deploy, setDeploy] = useState<DeployEntry[] | null>(null);
  const [kpi, setKpi] = useState<KpiSummary | null>(null);
  const [siem, setSiem] = useState<SiemTelemetry | null>(null);
  const [errors, setErrors] = useState<AdminErrors>({
    workers: false, crat: false, autonomy: false, remoteAgents: false, deploy: false, kpi: false, siem: false,
  });

  async function reloadAutonomy() {
    try {
      const updated = await fetch("/api/config/autonomy").then((r) => r.json());
      if (!isErr(updated)) setAutonomy(updated as AutonomyPolicyResponse);
    } catch {
      /* keep prior */
    }
  }

  useEffect(() => {
    async function load() {
      const p = `tenant_id=${encodeURIComponent(tenant)}`;
      const [wRes, cRes, auRes, raRes, dRes, kRes, sRes] = await Promise.allSettled([
        fetch(`/api/workers?${p}`).then((r) => r.json()),
        fetch(`/api/crat?${p}`).then((r) => r.json()),
        fetch(`/api/config/autonomy`).then((r) => r.json()),
        fetch(`/api/remote-agents`).then((r) => r.json()),
        fetch(`/api/deploy`).then((r) => r.json()),
        fetch(`/api/kpi?${p}`).then((r) => r.json()),
        fetch(`/api/siem/overview`).then((r) => r.json()),
      ]);
      const nextErrors: AdminErrors = {
        workers: false, crat: false, autonomy: false, remoteAgents: false, deploy: false, kpi: false, siem: false,
      };

      if (wRes.status === "fulfilled" && !isErr(wRes.value)) setPods(workersToPodsMap(wRes.value as WorkersResponse));
      else nextErrors.workers = true;

      if (cRes.status === "fulfilled" && !isErr(cRes.value)) setCrat(cRes.value as CratResponse);
      else nextErrors.crat = true;

      if (auRes.status === "fulfilled" && !isErr(auRes.value)) setAutonomy(auRes.value as AutonomyPolicyResponse);
      else nextErrors.autonomy = true;

      if (raRes.status === "fulfilled" && !isErr(raRes.value)) setRemoteAgents(raRes.value as RemoteAgentsResponse);
      else nextErrors.remoteAgents = true;

      if (dRes.status === "fulfilled" && !isErr(dRes.value)) {
        const d = dRes.value as {
          components?: { name: string; role: string; current_version: string; status: string; last_deployed: string; replicas: number }[];
        };
        setDeploy(
          (d.components ?? []).map((c) => ({
            name: c.name,
            role: c.role,
            version: c.current_version,
            status: c.status as DeployEntry["status"],
            last_deployed: c.last_deployed,
            replicas: c.replicas,
          }))
        );
      } else nextErrors.deploy = true;

      if (kRes.status === "fulfilled" && !isErr(kRes.value)) {
        const k = kRes.value as {
          advisory: { accepted: number; rejected: number; total: number; acceptance_rate: number | null };
          execution: { false_positive: number; false_positive_rate: number | null };
          trend: { lane: string; detected: number; resolved: number }[];
        };
        setKpi({
          acceptance_rate: k.advisory.acceptance_rate,
          false_positive_rate: k.execution.false_positive_rate,
          accepted: k.advisory.accepted,
          total: k.advisory.total,
          fp_count: k.execution.false_positive,
          trend: k.trend,
          source: "gateway",
        });
      } else nextErrors.kpi = true;

      if (sRes.status === "fulfilled" && !isErr(sRes.value)) {
        const s = sRes.value as {
          llm?: SiemTelemetry["llm"];
          rag?: SiemTelemetry["rag"];
          pipeline?: SiemTelemetry["pipeline"];
        };
        setSiem({
          llm: s.llm ?? { total_calls_24h: null, success_rate: null, latency_p50_ms: null, latency_p95_ms: null, tokens_in_total: null, tokens_out_total: null },
          rag: s.rag ?? { queries_24h: null, cache_hit_ratio: null, avg_query_latency_ms: null },
          pipeline: s.pipeline ?? { kafka_lag: [], redis_ops_per_sec: null, redis_memory_used_bytes: null, redis_memory_max_bytes: null },
          source: "prometheus",
        });
      } else nextErrors.siem = true;

      setErrors(nextErrors);
    }
    void load();
    const t = setInterval(() => void load(), 30_000);
    return () => clearInterval(t);
  }, [tenant]);

  return { pods, crat, autonomy, remoteAgents, deploy, kpi, siem, errors, reloadAutonomy };
}
