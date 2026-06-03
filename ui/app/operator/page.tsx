"use client";

import { useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { Sidebar } from "@/components/sidebar";
import { TenantSelector } from "@/components/tenant-selector";
import { Radio } from "lucide-react";
import { useOperatorData } from "@/components/operator/useOperatorData";
import { IncidentList } from "@/components/operator/IncidentList";
import { OverviewPanel } from "@/components/operator/OverviewPanel";
import { AdvisoryPanel } from "@/components/operator/AdvisoryPanel";

function OperatorPageInner() {
  const searchParams = useSearchParams();
  const tenant = searchParams.get("tenant") ?? "default";

  const [selected, setSelected] = useState<string | null>(null);
  const [prevTenant, setPrevTenant] = useState(tenant);
  const data = useOperatorData(tenant);

  // Reset selection when tenant changes (adjust-state-during-render pattern).
  if (tenant !== prevTenant) {
    setPrevTenant(tenant);
    setSelected(null);
  }

  const allIncidents = data.incidents ?? [];
  const activeCount = allIncidents.filter((i) => i.status !== "RESOLVED").length;
  const hitlCount = data.hitlItems.length;
  const selectedIncident = allIncidents.find((i) => i.id === selected);

  return (
    <div className="flex h-full">
      <Sidebar />
      <main className="flex-1 overflow-hidden bg-zinc-950 font-mono flex flex-col">

        {/* Header */}
        <div className="sticky top-0 z-10 flex items-center justify-between px-4 h-9 border-b border-zinc-800 bg-zinc-950 shrink-0">
          <div className="flex items-center gap-3 text-[10px]">
            <Radio size={10} className="text-emerald-400 animate-pulse" />
            <span className="text-zinc-300 font-semibold tracking-widest uppercase">Operator</span>
            {data.incidents !== null ? (
              <span className="text-zinc-600">
                {activeCount > 0 ? <span className="text-amber-400">{activeCount} active</span> : <span className="text-emerald-400">0 active</span>}
                <span> · {tenant}</span>
              </span>
            ) : (
              <span className="text-zinc-600 animate-pulse">loading…</span>
            )}
          </div>
          <div className="flex items-center gap-3 text-[10px]">
            <TenantSelector />
            {hitlCount > 0 && (
              <span className="flex items-center gap-1.5 text-rose-400 border border-rose-500/30 bg-rose-500/5 px-2 py-0.5 rounded">
                <span className="w-1.5 h-1.5 rounded-full bg-rose-400 animate-pulse" />
                {hitlCount} HITL
              </span>
            )}
          </div>
        </div>

        <div className="flex-1 overflow-hidden flex">
          <IncidentList incidents={data.incidents} selected={selected} error={data.errors.incidents} onSelect={setSelected} />

          <div className="flex-1 overflow-y-auto">
            {selectedIncident ? (
              <AdvisoryPanel incident={selectedIncident} />
            ) : (
              <OverviewPanel
                kpi={data.kpi}
                lanes={data.lanes}
                hitlItems={data.hitlItems}
                hitlDecisions={data.hitlDecisions}
                decideHitl={data.decideHitl}
                alertForm={data.alertForm}
                setAlertForm={data.setAlertForm}
                alertStatus={data.alertStatus}
                sendAlert={data.sendAlert}
                siemCorrelation={data.siemCorrelation}
                siemPlaybook={data.siemPlaybook}
                siemPipeline={data.siemPipeline}
                kpiError={data.errors.kpi}
                siemError={data.errors.siem}
              />
            )}
          </div>
        </div>
      </main>
    </div>
  );
}

export default function OperatorPage() {
  return (
    <Suspense>
      <OperatorPageInner />
    </Suspense>
  );
}
