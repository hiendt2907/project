"use client";

import { useEffect, useState, useCallback } from "react";
import { Sidebar } from "@/components/sidebar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  Settings,
  RefreshCw,
  Clock,
  AlertTriangle,
  CheckCircle2,
  RotateCcw,
} from "lucide-react";
import type { AutonomyPolicyResponse, PolicyRule, AutonomyLevel } from "@/app/api/config/autonomy/route";

const LANES = ["SYS_RESOURCE", "SYS_HARD_FAIL", "APP_HTTP", "SIEM_SECURITY"] as const;
const SEVERITIES = ["critical", "high", "medium", "low"] as const;
const LEVELS: AutonomyLevel[] = ["FULL_AUTO", "HITL", "SUGGEST_ONLY", "ALERT_ONLY"];

const LANE_LABELS: Record<string, string> = {
  SYS_RESOURCE: "Resource",
  SYS_HARD_FAIL: "Hard Fail",
  APP_HTTP: "HTTP",
  SIEM_SECURITY: "SIEM Security",
};

const LEVEL_BADGE: Record<AutonomyLevel, string> = {
  FULL_AUTO: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  HITL: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  SUGGEST_ONLY: "bg-blue-500/15 text-blue-400 border-blue-500/30",
  ALERT_ONLY: "bg-zinc-700/50 text-zinc-400 border-zinc-700",
};

const LEVEL_DESCRIPTIONS: Record<AutonomyLevel, string> = {
  FULL_AUTO: "Automatically execute remediation without human review",
  HITL: "Pause for human approval before executing",
  SUGGEST_ONLY: "Generate advisory but do not execute",
  ALERT_ONLY: "Send alert only — no advisory or execution",
};

interface CellEdit {
  lane: string;
  severity: string;
  level: AutonomyLevel;
}

function getLevel(rules: PolicyRule[], lane: string, severity: string): AutonomyLevel {
  const rule = rules.find((r) => r.lane === lane && r.severity === severity);
  return rule?.level ?? "ALERT_ONLY";
}

export default function AutonomyPage() {
  const [data, setData] = useState<AutonomyPolicyResponse | null>(null);
  const [pendingEdits, setPendingEdits] = useState<CellEdit[]>([]);
  const [saving, setSaving] = useState(false);
  const [saveResult, setSaveResult] = useState<"success" | "error" | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [cellMenu, setCellMenu] = useState<{ lane: string; severity: string } | null>(null);
  const [lastRefresh, setLastRefresh] = useState(new Date());

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/config/autonomy", { cache: "no-store" });
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
  }, [load]);

  function getEffectiveLevel(lane: string, severity: string): AutonomyLevel {
    const pending = pendingEdits.find((e) => e.lane === lane && e.severity === severity);
    if (pending) return pending.level;
    if (!data) return "ALERT_ONLY";
    return getLevel(data.rules, lane, severity);
  }

  function handleCellChange(lane: string, severity: string, level: AutonomyLevel) {
    setPendingEdits((prev) => {
      const existing = prev.findIndex((e) => e.lane === lane && e.severity === severity);
      if (existing >= 0) {
        const updated = [...prev];
        updated[existing] = { lane, severity, level };
        return updated;
      }
      return [...prev, { lane, severity, level }];
    });
    setCellMenu(null);
  }

  async function handleSave() {
    if (pendingEdits.length === 0) return;
    setSaving(true);
    setSaveResult(null);
    try {
      const results = await Promise.all(
        pendingEdits.map((edit) =>
          fetch("/api/config/autonomy", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(edit),
          })
        )
      );
      const allOk = results.every((r) => r.ok);
      setSaveResult(allOk ? "success" : "error");
      if (allOk) {
        setPendingEdits([]);
        await load();
      }
    } catch {
      setSaveResult("error");
    } finally {
      setSaving(false);
      setTimeout(() => setSaveResult(null), 3000);
    }
  }

  function handleReset() {
    setPendingEdits([]);
    setResetOpen(false);
  }

  const hasPending = pendingEdits.length > 0;

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        <header className="sticky top-0 z-10 flex h-16 items-center justify-between border-b border-zinc-800 bg-zinc-950/80 px-6 backdrop-blur">
          <div className="flex items-center gap-3">
            <h1 className="text-base font-semibold text-zinc-100">Autonomy Policy</h1>
            {data && (
              <span className="text-[11px] text-zinc-600">
                last modified {new Date(data.last_modified).toLocaleDateString()} by {data.modified_by}
              </span>
            )}
          </div>
          <div className="flex items-center gap-3">
            {hasPending && (
              <span className="text-[11px] text-amber-400">
                {pendingEdits.length} unsaved change{pendingEdits.length > 1 ? "s" : ""}
              </span>
            )}
            {saveResult === "success" && (
              <span className="flex items-center gap-1 text-[11px] text-emerald-400">
                <CheckCircle2 className="h-3.5 w-3.5" /> Saved
              </span>
            )}
            {saveResult === "error" && (
              <span className="flex items-center gap-1 text-[11px] text-rose-400">
                <AlertTriangle className="h-3.5 w-3.5" /> Save failed
              </span>
            )}
            {hasPending && (
              <button
                onClick={() => void handleSave()}
                disabled={saving}
                className="rounded bg-cyan-500 px-3 py-1.5 text-xs font-semibold text-zinc-950 hover:bg-cyan-400 disabled:opacity-50 transition-colors"
              >
                {saving ? "Saving…" : "Save Changes"}
              </button>
            )}
            <span className="flex items-center gap-1 text-xs text-zinc-600">
              <RefreshCw className="h-3 w-3" />
              {lastRefresh.toLocaleTimeString()}
            </span>
          </div>
        </header>

        <div className="p-6 space-y-6">
          {/* Policy matrix */}
          <Card className="border-zinc-800 bg-zinc-900/60">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-zinc-300">
                <Settings className="mr-2 inline h-3.5 w-3.5 text-zinc-500" />
                Policy Matrix
              </CardTitle>
            </CardHeader>
            <CardContent>
              {!data ? (
                <Skeleton className="h-48 w-full bg-zinc-800" />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full">
                    <thead>
                      <tr className="border-b border-zinc-800">
                        <th className="pb-3 pr-6 text-left text-[10px] uppercase tracking-widest text-zinc-500">
                          Lane
                        </th>
                        {SEVERITIES.map((sev) => (
                          <th key={sev} className="pb-3 px-4 text-center text-[10px] uppercase tracking-widest text-zinc-500">
                            {sev}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-zinc-800/60">
                      {LANES.map((lane) => (
                        <tr key={lane} className="hover:bg-zinc-800/20 transition-colors">
                          <td className="py-3 pr-6">
                            <div>
                              <p className="text-xs font-medium text-zinc-300">{LANE_LABELS[lane]}</p>
                              <p className="font-mono text-[9px] text-zinc-600">{lane}</p>
                            </div>
                          </td>
                          {SEVERITIES.map((sev) => {
                            const level = getEffectiveLevel(lane, sev);
                            const isPending = pendingEdits.some((e) => e.lane === lane && e.severity === sev);
                            const isMenuOpen = cellMenu?.lane === lane && cellMenu?.severity === sev;

                            return (
                              <td key={sev} className="py-3 px-4 text-center">
                                <div className="relative inline-block">
                                  <button
                                    onClick={() => setCellMenu(isMenuOpen ? null : { lane, severity: sev })}
                                    className={`group relative rounded border px-2 py-1 text-[10px] font-medium uppercase tracking-wide transition-colors hover:ring-1 hover:ring-zinc-500 ${LEVEL_BADGE[level]} ${isPending ? "ring-1 ring-amber-500/50" : ""}`}
                                    title="Click to edit"
                                  >
                                    {level.replace("_", " ")}
                                    {isPending && (
                                      <span className="ml-1 h-1.5 w-1.5 rounded-full bg-amber-400 inline-block" />
                                    )}
                                  </button>

                                  {isMenuOpen && (
                                    <div className="absolute left-1/2 top-full z-20 mt-1 -translate-x-1/2 w-48 rounded-lg border border-zinc-700 bg-zinc-900 p-1 shadow-xl">
                                      {LEVELS.map((opt) => (
                                        <button
                                          key={opt}
                                          onClick={() => handleCellChange(lane, sev, opt)}
                                          className={`w-full rounded px-3 py-2 text-left text-[11px] transition-colors hover:bg-zinc-800 ${opt === level ? "text-zinc-100 bg-zinc-800/60" : "text-zinc-400"}`}
                                        >
                                          <div className="font-medium">{opt.replace(/_/g, " ")}</div>
                                          <div className="text-[9px] text-zinc-600 mt-0.5">{LEVEL_DESCRIPTIONS[opt]}</div>
                                        </button>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div className="mt-4 flex flex-wrap gap-3 border-t border-zinc-800 pt-3">
                {LEVELS.map((level) => (
                  <span key={level} className={`rounded border px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide ${LEVEL_BADGE[level]}`}>
                    {level.replace(/_/g, " ")} — {LEVEL_DESCRIPTIONS[level]}
                  </span>
                ))}
              </div>
            </CardContent>
          </Card>

          {/* Change history */}
          <Card className="border-zinc-800 bg-zinc-900/60">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-zinc-300">Change History</CardTitle>
            </CardHeader>
            <CardContent>
              {!data ? (
                <Skeleton className="h-32 w-full bg-zinc-800" />
              ) : data.history.length === 0 ? (
                <p className="text-xs text-zinc-600">No recent changes</p>
              ) : (
                <div className="space-y-2">
                  {data.history.map((change) => (
                    <div
                      key={change.id}
                      className="flex items-center gap-4 rounded-lg border border-zinc-800/60 bg-zinc-950/40 px-3 py-2.5"
                    >
                      <Clock className="h-3.5 w-3.5 shrink-0 text-zinc-600" />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <code className="font-mono text-[11px] text-zinc-400">{change.lane}</code>
                          <span className="text-[10px] text-zinc-600">·</span>
                          <code className="font-mono text-[11px] text-zinc-400">{change.severity}</code>
                          <span className="text-[10px] text-zinc-600">·</span>
                          <span className={`rounded border px-1 py-0.5 text-[9px] font-medium ${LEVEL_BADGE[change.old_level]}`}>
                            {change.old_level.replace(/_/g, " ")}
                          </span>
                          <span className="text-[10px] text-zinc-600">→</span>
                          <span className={`rounded border px-1 py-0.5 text-[9px] font-medium ${LEVEL_BADGE[change.new_level]}`}>
                            {change.new_level.replace(/_/g, " ")}
                          </span>
                        </div>
                      </div>
                      <div className="shrink-0 text-right">
                        <p className="font-mono text-[10px] text-zinc-500">{change.operator}</p>
                        <p className="font-mono text-[9px] text-zinc-700">
                          {new Date(change.timestamp).toLocaleDateString()}
                        </p>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          {/* Danger zone */}
          <Card className="border-rose-500/20 bg-rose-500/5">
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm font-medium text-rose-400">
                <AlertTriangle className="h-4 w-4" />
                Danger Zone
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-zinc-300">Reset to Defaults</p>
                  <p className="text-xs text-zinc-500 mt-0.5">
                    Revert all policy rules to system defaults. This cannot be undone.
                  </p>
                </div>
                <button
                  onClick={() => setResetOpen(true)}
                  className="flex items-center gap-2 rounded border border-rose-500/30 bg-rose-500/10 px-3 py-1.5 text-xs font-medium text-rose-400 hover:bg-rose-500/20 transition-colors"
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                  Reset to Defaults
                </button>
              </div>
            </CardContent>
          </Card>
        </div>
      </main>

      {/* Confirmation dialog */}
      <Dialog open={resetOpen} onOpenChange={setResetOpen}>
        <DialogContent className="max-w-md border-zinc-800 bg-zinc-900 text-zinc-100">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-rose-400">
              <AlertTriangle className="h-4 w-4" />
              Confirm Reset
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <p className="text-sm text-zinc-300">
              This will discard all pending changes and reset the policy to defaults. Are you sure?
            </p>
            <div className="flex gap-3">
              <button
                onClick={handleReset}
                className="flex-1 rounded bg-rose-600 px-4 py-2 text-sm font-semibold text-white hover:bg-rose-500 transition-colors"
              >
                Yes, Reset
              </button>
              <button
                onClick={() => setResetOpen(false)}
                className="flex-1 rounded border border-zinc-700 px-4 py-2 text-sm font-semibold text-zinc-300 hover:bg-zinc-800 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* Click outside to close cell menu */}
      {cellMenu && (
        <div
          className="fixed inset-0 z-10"
          onClick={() => setCellMenu(null)}
        />
      )}
    </div>
  );
}
