"use client";

import { useEffect, useState, useCallback } from "react";
import { Sidebar } from "@/components/sidebar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  Bell,
  RefreshCw,
  CheckCircle2,
  XCircle,
  Clock,
  Copy,
  ChevronRight,
  AlertTriangle,
} from "lucide-react";
import type { Incident, IncidentsResponse, IncidentStatus, IncidentSeverity, IncidentLane } from "@/app/api/incidents/route";

const LANE_BADGE: Record<IncidentLane, string> = {
  SYS_RESOURCE: "bg-cyan-500/15 text-cyan-400 border-cyan-500/30",
  SYS_HARD_FAIL: "bg-rose-500/15 text-rose-400 border-rose-500/30",
  APP_HTTP: "bg-orange-500/15 text-orange-400 border-orange-500/30",
  SIEM_SECURITY: "bg-violet-500/15 text-violet-400 border-violet-500/30",
};

const LANE_LABEL: Record<IncidentLane, string> = {
  SYS_RESOURCE: "Resource",
  SYS_HARD_FAIL: "Hard Fail",
  APP_HTTP: "HTTP",
  SIEM_SECURITY: "SIEM",
};

const SEVERITY_BADGE: Record<IncidentSeverity, string> = {
  critical: "bg-rose-500/15 text-rose-400 border-rose-500/30",
  high: "bg-orange-500/15 text-orange-400 border-orange-500/30",
  medium: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  low: "bg-sky-500/15 text-sky-400 border-sky-500/30",
};

const VERDICT_COLOR: Record<string, string> = {
  CRITICAL: "text-rose-400",
  URGENT: "text-orange-400",
  INVESTIGATE: "text-amber-400",
  NORMAL: "text-emerald-400",
};

function StatusBadge({ status }: { status: IncidentStatus }) {
  if (status === "HITL_PENDING") {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-amber-500/30 bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-400">
        <span className="relative flex h-1.5 w-1.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75" />
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-amber-400" />
        </span>
        HITL Pending
      </span>
    );
  }
  if (status === "ACTIVE") {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-blue-500/30 bg-blue-500/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-blue-400">
        <span className="relative flex h-1.5 w-1.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-400 opacity-75" />
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-blue-400" />
        </span>
        Active
      </span>
    );
  }
  if (status === "RESOLVED") {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-emerald-500/30 bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-emerald-400">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
        Resolved
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded border border-rose-500/30 bg-rose-500/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-rose-400">
      <span className="h-1.5 w-1.5 rounded-full bg-rose-400" />
      Failed
    </span>
  );
}

function DetailPanel({
  incident,
  onClose,
  onApprove,
  onReject,
}: {
  incident: Incident;
  onClose: () => void;
  onApprove: (id: string) => Promise<void>;
  onReject: (id: string) => Promise<void>;
}) {
  const [copied, setCopied] = useState(false);
  const [deciding, setDeciding] = useState(false);

  function copyHash() {
    void navigator.clipboard.writeText(incident.crat_hash);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  async function handleApprove() {
    if (!incident.hitl_incident_id) return;
    setDeciding(true);
    await onApprove(incident.hitl_incident_id);
    setDeciding(false);
    onClose();
  }

  async function handleReject() {
    if (!incident.hitl_incident_id) return;
    setDeciding(true);
    await onReject(incident.hitl_incident_id);
    setDeciding(false);
    onClose();
  }

  return (
    <DialogContent className="max-w-2xl border-zinc-800 bg-zinc-900 text-zinc-100">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2 text-sm font-semibold text-zinc-100">
          <ChevronRight className="h-4 w-4 text-zinc-500" />
          Incident {incident.id}
        </DialogTitle>
      </DialogHeader>

      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${LANE_BADGE[incident.lane]}`}>
            {LANE_LABEL[incident.lane]}
          </span>
          <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${SEVERITY_BADGE[incident.severity]}`}>
            {incident.severity}
          </span>
          <StatusBadge status={incident.status} />
        </div>

        <p className="text-sm text-zinc-300">{incident.summary}</p>

        <div className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 space-y-1">
          <p className="text-[10px] uppercase tracking-widest text-zinc-500 mb-2">Timeline</p>
          {incident.events.map((ev, i) => (
            <div key={i} className="flex items-start gap-3">
              <span className="mt-0.5 font-mono text-[10px] text-zinc-600 shrink-0 w-20">
                {new Date(ev.timestamp).toLocaleTimeString()}
              </span>
              <div className="flex items-start gap-1.5">
                <div className="mt-1.5 h-1.5 w-1.5 rounded-full bg-zinc-600 shrink-0" />
                <p className="text-[11px] text-zinc-400">{ev.message}</p>
              </div>
            </div>
          ))}
        </div>

        <div className="space-y-1.5">
          <p className="text-[10px] uppercase tracking-widest text-zinc-500">CRAT Block Hash</p>
          <div className="flex items-center gap-2 rounded border border-zinc-800 bg-zinc-950/60 px-3 py-2">
            <code className="flex-1 font-mono text-[11px] text-zinc-400 truncate">{incident.crat_hash}</code>
            <button
              onClick={copyHash}
              className="shrink-0 text-zinc-600 hover:text-zinc-300 transition-colors"
            >
              <Copy className="h-3.5 w-3.5" />
            </button>
            {copied && <span className="text-[10px] text-emerald-400">Copied!</span>}
          </div>
        </div>

        {incident.playbook_id && (
          <div className="flex items-center gap-2 text-[11px] text-zinc-500">
            <span className="text-zinc-600">Playbook:</span>
            <code className="font-mono text-zinc-400">{incident.playbook_id}</code>
          </div>
        )}

        {incident.status === "HITL_PENDING" && incident.hitl_incident_id && (
          <div className="flex gap-3 border-t border-zinc-800 pt-4">
            <button
              onClick={handleApprove}
              disabled={deciding}
              className="flex flex-1 items-center justify-center gap-2 rounded-md bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-500 disabled:opacity-50 transition-colors"
            >
              <CheckCircle2 className="h-4 w-4" />
              {deciding ? "Submitting…" : "Approve"}
            </button>
            <button
              onClick={handleReject}
              disabled={deciding}
              className="flex flex-1 items-center justify-center gap-2 rounded-md border border-rose-500/30 px-4 py-2 text-sm font-semibold text-rose-400 hover:bg-rose-500/10 disabled:opacity-50 transition-colors"
            >
              <XCircle className="h-4 w-4" />
              Reject
            </button>
          </div>
        )}
      </div>
    </DialogContent>
  );
}

export default function IncidentsPage() {
  const [data, setData] = useState<IncidentsResponse | null>(null);
  const [selected, setSelected] = useState<Incident | null>(null);
  const [laneFilter, setLaneFilter] = useState("all");
  const [severityFilter, setSeverityFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [lastRefresh, setLastRefresh] = useState(new Date());

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/incidents", { cache: "no-store" });
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
    const t = setInterval(load, 5_000);
    return () => clearInterval(t);
  }, [load]);

  async function handleApprove(incidentId: string) {
    await fetch("/api/hitl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ incident_id: incidentId, decision: "approved" }),
    });
    await load();
  }

  async function handleReject(incidentId: string) {
    await fetch("/api/hitl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ incident_id: incidentId, decision: "rejected" }),
    });
    await load();
  }

  const filtered = (data?.incidents ?? []).filter((inc) => {
    if (laneFilter !== "all" && inc.lane !== laneFilter) return false;
    if (severityFilter !== "all" && inc.severity !== severityFilter) return false;
    if (statusFilter !== "all" && inc.status !== statusFilter) return false;
    return true;
  });

  const hitlCount = data?.hitl_pending_count ?? 0;

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        <header className="sticky top-0 z-10 flex h-16 items-center justify-between border-b border-zinc-800 bg-zinc-950/80 px-6 backdrop-blur">
          <div className="flex items-center gap-3">
            <h1 className="text-base font-semibold text-zinc-100">Incidents</h1>
            {hitlCount > 0 && (
              <span className="relative inline-flex items-center gap-1 rounded-full bg-rose-500/15 border border-rose-500/30 px-2 py-0.5 text-xs font-semibold text-rose-400">
                <span className="absolute -top-0.5 -right-0.5 flex h-2 w-2">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-rose-400 opacity-75" />
                  <span className="relative inline-flex rounded-full h-2 w-2 bg-rose-400" />
                </span>
                <Bell className="h-3 w-3" />
                {hitlCount} HITL
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

        <div className="p-6 space-y-4">
          {/* Filter bar */}
          <div className="flex flex-wrap gap-3">
            <Select value={laneFilter} onValueChange={(v) => setLaneFilter(v ?? "all")}>
              <SelectTrigger className="w-36 border-zinc-800 bg-zinc-900 text-zinc-300 h-9 text-sm">
                <SelectValue placeholder="Lane" />
              </SelectTrigger>
              <SelectContent className="border-zinc-700 bg-zinc-900">
                {["all", "SYS_RESOURCE", "SYS_HARD_FAIL", "APP_HTTP", "SIEM_SECURITY"].map((v) => (
                  <SelectItem key={v} value={v} className="text-zinc-300 focus:bg-zinc-800">
                    {v === "all" ? "All Lanes" : LANE_LABEL[v as IncidentLane]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select value={severityFilter} onValueChange={(v) => setSeverityFilter(v ?? "all")}>
              <SelectTrigger className="w-36 border-zinc-800 bg-zinc-900 text-zinc-300 h-9 text-sm">
                <SelectValue placeholder="Severity" />
              </SelectTrigger>
              <SelectContent className="border-zinc-700 bg-zinc-900">
                {["all", "critical", "high", "medium", "low"].map((v) => (
                  <SelectItem key={v} value={v} className="text-zinc-300 focus:bg-zinc-800 capitalize">
                    {v === "all" ? "All Severities" : v}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v ?? "all")}>
              <SelectTrigger className="w-40 border-zinc-800 bg-zinc-900 text-zinc-300 h-9 text-sm">
                <SelectValue placeholder="Status" />
              </SelectTrigger>
              <SelectContent className="border-zinc-700 bg-zinc-900">
                {["all", "ACTIVE", "HITL_PENDING", "RESOLVED", "FAILED"].map((v) => (
                  <SelectItem key={v} value={v} className="text-zinc-300 focus:bg-zinc-800">
                    {v === "all" ? "All Statuses" : v.replace("_", " ")}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <span className="ml-auto text-xs text-zinc-600 self-center">
              {filtered.length} of {data?.total ?? 0} incidents
            </span>
          </div>

          {/* Incident table */}
          <Card className="border-zinc-800 bg-zinc-900/60">
            <CardContent className="p-0">
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-zinc-800 text-left text-[10px] uppercase tracking-widest text-zinc-500">
                      <th className="px-4 py-3">Time</th>
                      <th className="px-4 py-3">Lane</th>
                      <th className="px-4 py-3">Severity</th>
                      <th className="px-4 py-3">Verdict</th>
                      <th className="px-4 py-3">Status</th>
                      <th className="px-4 py-3">Playbook</th>
                      <th className="px-4 py-3">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-800/60">
                    {!data ? (
                      Array.from({ length: 6 }).map((_, i) => (
                        <tr key={i}>
                          <td colSpan={7} className="px-4 py-3">
                            <Skeleton className="h-4 w-full bg-zinc-800" />
                          </td>
                        </tr>
                      ))
                    ) : filtered.length === 0 ? (
                      <tr>
                        <td colSpan={7} className="px-4 py-10 text-center text-zinc-600">
                          <AlertTriangle className="mx-auto mb-2 h-6 w-6 opacity-30" />
                          No incidents match your filters
                        </td>
                      </tr>
                    ) : (
                      filtered.map((inc) => (
                        <tr
                          key={inc.id}
                          onClick={() => setSelected(inc)}
                          className="cursor-pointer hover:bg-zinc-800/40 transition-colors"
                        >
                          <td className="px-4 py-3 font-mono text-zinc-500 whitespace-nowrap">
                            <div className="flex items-center gap-1">
                              <Clock className="h-3 w-3" />
                              {new Date(inc.timestamp).toLocaleTimeString()}
                            </div>
                            <div className="text-[10px] text-zinc-700">
                              {new Date(inc.timestamp).toLocaleDateString()}
                            </div>
                          </td>
                          <td className="px-4 py-3">
                            <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${LANE_BADGE[inc.lane]}`}>
                              {LANE_LABEL[inc.lane]}
                            </span>
                          </td>
                          <td className="px-4 py-3">
                            <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${SEVERITY_BADGE[inc.severity]}`}>
                              {inc.severity}
                            </span>
                          </td>
                          <td className={`px-4 py-3 font-mono font-semibold ${VERDICT_COLOR[inc.verdict] ?? "text-zinc-400"}`}>
                            {inc.verdict}
                          </td>
                          <td className="px-4 py-3">
                            <StatusBadge status={inc.status} />
                          </td>
                          <td className="px-4 py-3 font-mono text-zinc-500">
                            {inc.playbook_id ?? <span className="text-zinc-700">—</span>}
                          </td>
                          <td className="px-4 py-3">
                            <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                              <button
                                onClick={() => setSelected(inc)}
                                className="rounded border border-zinc-700 px-2 py-1 text-[10px] text-zinc-400 hover:border-zinc-600 hover:text-zinc-200 transition-colors"
                              >
                                Details
                              </button>
                              {inc.status === "HITL_PENDING" && inc.hitl_incident_id && (
                                <>
                                  <button
                                    onClick={() => void handleApprove(inc.hitl_incident_id!)}
                                    className="rounded border border-emerald-500/30 bg-emerald-500/10 px-2 py-1 text-[10px] font-medium text-emerald-400 hover:bg-emerald-500/20 transition-colors"
                                  >
                                    Approve
                                  </button>
                                  <button
                                    onClick={() => void handleReject(inc.hitl_incident_id!)}
                                    className="rounded border border-rose-500/30 bg-rose-500/10 px-2 py-1 text-[10px] font-medium text-rose-400 hover:bg-rose-500/20 transition-colors"
                                  >
                                    Reject
                                  </button>
                                </>
                              )}
                            </div>
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

      <Dialog open={!!selected} onOpenChange={(open) => { if (!open) setSelected(null); }}>
        {selected && (
          <DetailPanel
            incident={selected}
            onClose={() => setSelected(null)}
            onApprove={handleApprove}
            onReject={handleReject}
          />
        )}
      </Dialog>
    </div>
  );
}
