"use client";

import { useEffect, useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { Sidebar } from "@/components/sidebar";
import { TenantSelector } from "@/components/tenant-selector";
import { Radio } from "lucide-react";
import { StatCard, SectionLabel } from "@/components/shared/primitives";
import { pct } from "@/components/shared/fmt";
import { PipelineFlow } from "@/components/shared/PipelineFlow";
import { TraceDetailDrawer } from "@/components/shared/TraceDetailDrawer";
import { useAdminData } from "@/components/admin/useAdminData";
import { WorkersPanel } from "@/components/admin/WorkersPanel";
import { KpiPanel } from "@/components/admin/KpiPanel";
import { CratPanel } from "@/components/admin/CratPanel";
import { LlmRagPanel } from "@/components/admin/LlmRagPanel";
import { AutonomyPanel } from "@/components/admin/AutonomyPanel";
import { RemoteAgentsPanel } from "@/components/admin/RemoteAgentsPanel";
import { DeployPanel } from "@/components/admin/DeployPanel";

function AdminPageInner() {
  const searchParams = useSearchParams();
  const tenant = searchParams.get("tenant") ?? "default";

  const [now, setNow] = useState(() => Date.now());
  const [trace, setTrace] = useState<string | null>(null);
  const { pods, crat, autonomy, remoteAgents, deploy, kpi, siem, errors, reloadAutonomy } = useAdminData(tenant);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(t);
  }, []);

  const healthy = (pods ?? []).filter((p) => p.status === "healthy").length;
  const degraded = (pods ?? []).filter((p) => p.status === "degraded").length;
  const down = (pods ?? []).filter((p) => p.status === "unhealthy").length;
  const maxKafkaLag = siem ? Math.max(0, ...siem.pipeline.kafka_lag.map((k) => k.lag)) : 0;

  return (
    <div className="flex h-full">
      <Sidebar />
      <main className="flex-1 overflow-auto bg-zinc-950 font-mono text-[11px]">

        {/* Header */}
        <div className="sticky top-0 z-10 flex items-center justify-between px-4 h-9 border-b border-zinc-800 bg-zinc-950">
          <div className="flex items-center gap-3 text-[10px]">
            <span className="text-amber-400 font-semibold tracking-widest uppercase">Admin</span>
            <span className="text-zinc-700">ns:multi-agent</span>
            {pods !== null && (
              <span className="flex items-center gap-1.5">
                <span className="text-emerald-400">{healthy} ok</span>
                {degraded > 0 && <span className="text-amber-400">{degraded} warn</span>}
                {down > 0 && <span className="text-rose-400">{down} down</span>}
              </span>
            )}
          </div>
          <div className="flex items-center gap-3 text-[10px]">
            <TenantSelector />
            <span className="text-zinc-600">{new Date(now).toLocaleTimeString()}</span>
            <span className="flex items-center gap-1 text-emerald-400">
              <Radio size={9} className="animate-pulse" />live
            </span>
          </div>
        </div>

        {/* T1 · Stat cards */}
        <div className="flex gap-px border-b border-zinc-800 bg-zinc-950">
          <StatCard label="workers" value={pods ? `${healthy}/${pods.length}` : "—"} color={down > 0 ? "text-rose-400" : degraded > 0 ? "text-amber-400" : "text-emerald-400"} sub="healthy" />
          <StatCard label="kafka lag" value={siem ? `${maxKafkaLag}` : "—"} color={maxKafkaLag >= 1000 ? "text-rose-400" : maxKafkaLag >= 100 ? "text-amber-400" : "text-emerald-400"} sub="max msgs" />
          <StatCard label="kpi acceptance" value={pct(kpi?.acceptance_rate ?? null)} color={kpi?.acceptance_rate != null ? (kpi.acceptance_rate >= 0.8 ? "text-emerald-400" : kpi.acceptance_rate >= 0.6 ? "text-amber-400" : "text-rose-400") : "text-zinc-600"} sub={`${kpi?.accepted ?? 0}/${kpi?.total ?? 0} adv`} />
          <StatCard label="crat blocks" value={crat ? `${crat.total ?? 0}` : "—"} color={crat ? "text-sky-400" : "text-zinc-600"} sub="24h" />
        </div>

        <div className="p-4 space-y-5">

          {/* T1 · Pipeline flow */}
          <div>
            <SectionLabel text="System Pipeline · click a CRAT block below for the session" />
            <PipelineFlow
              workers={(pods ?? []).map((p) => ({ role: p.name, status: p.status }))}
              kafkaLag={siem?.pipeline.kafka_lag ?? []}
            />
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
            <WorkersPanel pods={pods} siem={siem} error={errors.workers} />
            <KpiPanel kpi={kpi} error={errors.kpi} />
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
            <CratPanel crat={crat} now={now} error={errors.crat} onSelectTrace={setTrace} />
            <LlmRagPanel siem={siem} error={errors.siem} />
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
            <AutonomyPanel autonomy={autonomy} error={errors.autonomy} onSaved={reloadAutonomy} />
            <RemoteAgentsPanel remoteAgents={remoteAgents} tenant={tenant} now={now} error={errors.remoteAgents} />
          </div>

          <DeployPanel deploy={deploy} error={errors.deploy} />
        </div>
      </main>

      {/* T3 · Trace session drawer */}
      <TraceDetailDrawer traceId={trace} onClose={() => setTrace(null)} />
    </div>
  );
}

export default function AdminPage() {
  return (
    <Suspense>
      <AdminPageInner />
    </Suspense>
  );
}
