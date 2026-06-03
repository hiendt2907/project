"use client";

// Container hook for the operator dashboard — data fetching, polling, and the
// HITL / alert-injection mutations. Presentational panels receive props.

import { useCallback, useEffect, useState } from "react";
import type { DiagnosticLanesData } from "@/components/diagnostic-lanes";
import type { Incident } from "@/app/api/incidents/route";
import {
  mapIncident,
  kpiFromResponse,
  type OperatorIncident,
  type HitlItem,
  type KpiData,
  type SiemCorrelation,
  type SiemPlaybook,
  type SiemPipeline,
  type HitlDecisionState,
} from "./types";

export interface AlertForm {
  alertname: string;
  namespace: string;
  pod: string;
  severity: string;
  summary: string;
}

const DEFAULT_ALERT: AlertForm = {
  alertname: "HighCPUUsage",
  namespace: "multi-agent",
  pod: "nginx-test-7c886d4485-ph7rv",
  severity: "warning",
  summary: "Container nginx reports ~90% CPU vs 50m limit",
};

export interface OperatorErrors {
  incidents: boolean;
  siem: boolean;
  kpi: boolean;
}

function isErr(v: unknown): boolean {
  return (v as { source?: string } | null)?.source === "error";
}

export function useOperatorData(tenant: string) {
  const [incidents, setIncidents] = useState<OperatorIncident[] | null>(null);
  const [lanes, setLanes] = useState<DiagnosticLanesData | null>(null);
  const [hitlItems, setHitlItems] = useState<HitlItem[]>([]);
  const [hitlDecisions, setHitlDecisions] = useState<HitlDecisionState>({});
  const [kpi, setKpi] = useState<KpiData | null>(null);
  const [siemCorrelation, setSiemCorrelation] = useState<SiemCorrelation | null>(null);
  const [siemPlaybook, setSiemPlaybook] = useState<SiemPlaybook | null>(null);
  const [siemPipeline, setSiemPipeline] = useState<SiemPipeline | null>(null);
  const [alertForm, setAlertForm] = useState<AlertForm>(DEFAULT_ALERT);
  const [alertStatus, setAlertStatus] = useState<"idle" | "sending" | "ok" | "err">("idle");
  const [errors, setErrors] = useState<OperatorErrors>({ incidents: false, siem: false, kpi: false });

  useEffect(() => {
    async function load() {
      const p = `tenant_id=${encodeURIComponent(tenant)}`;
      const [incRes, siemRes, kpiRes] = await Promise.allSettled([
        fetch(`/api/incidents?${p}`).then((r) => r.json()),
        fetch(`/api/siem/overview`).then((r) => r.json()),
        fetch(`/api/kpi?${p}`).then((r) => r.json()),
      ]);
      const nextErrors: OperatorErrors = { incidents: false, siem: false, kpi: false };

      if (incRes.status === "fulfilled" && !isErr(incRes.value)) {
        const d = incRes.value as { incidents?: Incident[] };
        setIncidents((d.incidents ?? []).map(mapIncident));
      } else {
        setIncidents([]);
        nextErrors.incidents = true;
      }

      if (siemRes.status === "fulfilled" && !isErr(siemRes.value)) {
        const d = siemRes.value as {
          diagnostic_lanes?: DiagnosticLanesData;
          hitl?: { queue?: HitlItem[] };
          correlation?: SiemCorrelation;
          playbook?: SiemPlaybook;
          pipeline?: SiemPipeline;
        };
        if (d.diagnostic_lanes) setLanes(d.diagnostic_lanes);
        if (d.hitl?.queue) setHitlItems(d.hitl.queue);
        if (d.correlation) setSiemCorrelation(d.correlation);
        if (d.playbook) setSiemPlaybook(d.playbook);
        if (d.pipeline) setSiemPipeline(d.pipeline);
      } else {
        nextErrors.siem = true;
      }

      if (kpiRes.status === "fulfilled" && !isErr(kpiRes.value)) {
        setKpi(kpiFromResponse(kpiRes.value as Parameters<typeof kpiFromResponse>[0]));
      } else {
        nextErrors.kpi = true;
      }

      setErrors(nextErrors);
    }
    void load();
    const t = setInterval(() => void load(), 30_000);
    return () => clearInterval(t);
  }, [tenant]);

  const decideHitl = useCallback(
    async (incident_id: string, trace_id: string, decision: "approved" | "rejected") => {
      setHitlDecisions((prev) => ({ ...prev, [incident_id]: "pending" }));
      try {
        const res = await fetch("/api/hitl", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ incident_id, decision, trace_id }),
        });
        setHitlDecisions((prev) => ({ ...prev, [incident_id]: res.ok ? decision : "error" }));
      } catch {
        setHitlDecisions((prev) => ({ ...prev, [incident_id]: "error" }));
      }
    },
    []
  );

  const sendAlert = useCallback(async () => {
    setAlertStatus("sending");
    try {
      const res = await fetch("/api/alerts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          receiver: "omni-webhook",
          status: "firing",
          alerts: [
            {
              labels: {
                alertname: alertForm.alertname,
                severity: alertForm.severity,
                pod: alertForm.pod,
                namespace: alertForm.namespace,
              },
              annotations: { summary: alertForm.summary },
              status: "firing",
            },
          ],
        }),
      });
      setAlertStatus(res.ok ? "ok" : "err");
    } catch {
      setAlertStatus("err");
    }
    setTimeout(() => setAlertStatus("idle"), 3000);
  }, [alertForm]);

  return {
    incidents,
    lanes,
    hitlItems,
    hitlDecisions,
    decideHitl,
    kpi,
    siemCorrelation,
    siemPlaybook,
    siemPipeline,
    alertForm,
    setAlertForm,
    alertStatus,
    sendAlert,
    errors,
  };
}
