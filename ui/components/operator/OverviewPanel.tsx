import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import { Send } from "lucide-react";
import { Unavailable } from "@/components/shared/primitives";
import { age, pct, fmtBytes } from "@/components/shared/fmt";
import { DiagnosticLanes, type DiagnosticLanesData } from "@/components/diagnostic-lanes";
import type { AlertForm } from "./useOperatorData";
import type {
  KpiData,
  HitlItem,
  HitlDecisionState,
  SiemCorrelation,
  SiemPlaybook,
  SiemPipeline,
} from "./types";

interface OverviewPanelProps {
  kpi: KpiData | null;
  lanes: DiagnosticLanesData | null;
  hitlItems: HitlItem[];
  hitlDecisions: HitlDecisionState;
  decideHitl: (id: string, trace: string, dec: "approved" | "rejected") => void;
  alertForm: AlertForm;
  setAlertForm: React.Dispatch<React.SetStateAction<AlertForm>>;
  alertStatus: "idle" | "sending" | "ok" | "err";
  sendAlert: () => void;
  siemCorrelation: SiemCorrelation | null;
  siemPlaybook: SiemPlaybook | null;
  siemPipeline: SiemPipeline | null;
  kpiError?: boolean;
  siemError?: boolean;
}

export function OverviewPanel({
  kpi, lanes, hitlItems, hitlDecisions, decideHitl,
  alertForm, setAlertForm, alertStatus, sendAlert,
  siemCorrelation, siemPlaybook, siemPipeline,
  kpiError, siemError,
}: OverviewPanelProps) {
  const barData =
    kpi?.trend_by_lane.map((t) => ({
      lane: t.lane.replace("SYS_", "").replace("SIEM_", "S:"),
      D: t.detected,
      R: t.resolved,
    })) ?? [];

  return (
    <div className="divide-y divide-zinc-800/40 text-[10px] font-mono">
      {/* KPI */}
      <div className="px-4 py-3">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-[9px] text-zinc-600 uppercase tracking-wider">KPI 24h</span>
          {kpi && (
            <span className="text-[8px] px-1 rounded border text-emerald-400 border-emerald-400/20">gateway</span>
          )}
        </div>
        {kpiError && !kpi ? (
          <Unavailable detail="KPI unavailable (gateway /kpi)" />
        ) : (
        <div className="flex items-end gap-6 flex-wrap">
          <div>
            <div className={`text-2xl font-bold tabular-nums leading-none ${kpi ? "text-emerald-400" : "text-zinc-700"}`}>{pct(kpi?.acceptance_rate ?? null)}</div>
            <p className="text-[9px] text-zinc-600 mt-0.5">acceptance · {kpi?.accepted ?? 0}/{kpi?.total_24h ?? 0}</p>
          </div>
          <div>
            <div className={`text-2xl font-bold tabular-nums leading-none ${kpi ? "text-rose-400" : "text-zinc-700"}`}>{pct(kpi?.false_positive_rate ?? null)}</div>
            <p className="text-[9px] text-zinc-600 mt-0.5">false positive</p>
          </div>
          {barData.length > 0 && (
            <div className="flex-1 min-w-[160px] h-16">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={barData} barCategoryGap="30%">
                  <XAxis dataKey="lane" tick={{ fontSize: 8, fill: "#52525b", fontFamily: "monospace" }} />
                  <YAxis tick={{ fontSize: 8, fill: "#52525b" }} width={18} />
                  <Tooltip contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", fontSize: 10, fontFamily: "monospace" }} labelStyle={{ color: "#a1a1aa" }} />
                  <Bar dataKey="D" name="Detected" fill="#f59e0b" radius={[1, 1, 0, 0]} />
                  <Bar dataKey="R" name="Resolved" fill="#6366f1" radius={[1, 1, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>
        )}
      </div>

      {/* SIEM telemetry unavailable banner */}
      {siemError && !lanes && !siemCorrelation && !siemPlaybook && !siemPipeline && (
        <div className="px-4 py-3">
          <p className="text-[9px] text-zinc-600 uppercase tracking-wider mb-2">SIEM Telemetry</p>
          <Unavailable detail="diagnostic lanes / correlation / playbook / pipeline unavailable (SIEM_METRICS_URL)" />
        </div>
      )}

      {/* Diagnostic Lanes */}
      {lanes && (
        <div className="px-4 py-3">
          <p className="text-[9px] text-zinc-600 uppercase tracking-wider mb-2">Diagnostic Lanes</p>
          <DiagnosticLanes data={lanes} />
        </div>
      )}

      {/* SIEM Correlation */}
      {siemCorrelation && (
        <div className="px-4 py-3">
          <p className="text-[9px] text-zinc-600 uppercase tracking-wider mb-2">SIEM Correlation 24h</p>
          <div className="flex items-end gap-6 mb-2">
            <div>
              <div className={`text-lg font-bold tabular-nums leading-none ${siemCorrelation.chains_detected_24h > 0 ? "text-violet-400" : "text-zinc-600"}`}>{siemCorrelation.chains_detected_24h}</div>
              <p className="text-[9px] text-zinc-600 mt-0.5">chains/24h</p>
            </div>
            <div>
              <div className={`text-lg font-bold tabular-nums leading-none ${siemCorrelation.active_windows > 0 ? "text-amber-400" : "text-zinc-600"}`}>{siemCorrelation.active_windows}</div>
              <p className="text-[9px] text-zinc-600 mt-0.5">active windows</p>
            </div>
          </div>
          {siemCorrelation.chains_by_category.length > 0 && (
            <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
              {siemCorrelation.chains_by_category.slice(0, 6).map((c) => (
                <div key={c.category} className="flex items-center justify-between text-[10px]">
                  <span className="text-zinc-500 truncate">{c.category.replace("_", " ")}</span>
                  <span className="text-zinc-400 tabular-nums ml-2">{c.count}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Playbook Stats */}
      {siemPlaybook && (
        <div className="px-4 py-3">
          <p className="text-[9px] text-zinc-600 uppercase tracking-wider mb-2">Playbook 24h</p>
          <div className="grid grid-cols-4 gap-3">
            <div><div className="text-base font-bold tabular-nums leading-none text-zinc-300">{siemPlaybook.matches_24h}</div><p className="text-[9px] text-zinc-600 mt-0.5">matched</p></div>
            <div><div className="text-base font-bold tabular-nums leading-none text-emerald-400">{siemPlaybook.auto_executed}</div><p className="text-[9px] text-zinc-600 mt-0.5">auto-exec</p></div>
            <div><div className="text-base font-bold tabular-nums leading-none text-orange-400">{siemPlaybook.hitl_gated}</div><p className="text-[9px] text-zinc-600 mt-0.5">hitl-gated</p></div>
            <div><div className="text-base font-bold tabular-nums leading-none text-zinc-500">{siemPlaybook.no_match}</div><p className="text-[9px] text-zinc-600 mt-0.5">no-match</p></div>
          </div>
        </div>
      )}

      {/* Pipeline */}
      {siemPipeline && siemPipeline.kafka_lag.length > 0 && (
        <div className="px-4 py-3">
          <p className="text-[9px] text-zinc-600 uppercase tracking-wider mb-2">Pipeline</p>
          <table className="w-full text-[10px] border-collapse mb-2">
            <thead>
              <tr>
                <th className="text-left pb-0.5 pr-3 text-zinc-700 font-normal text-[9px]">topic</th>
                <th className="text-left pb-0.5 pr-3 text-zinc-700 font-normal text-[9px]">group</th>
                <th className="text-right pb-0.5 text-zinc-700 font-normal text-[9px]">lag</th>
              </tr>
            </thead>
            <tbody>
              {siemPipeline.kafka_lag.slice(0, 5).map((k) => (
                <tr key={`${k.topic}-${k.group}`}>
                  <td className="py-0.5 pr-3 text-zinc-500 truncate max-w-[150px]">{k.topic}</td>
                  <td className="py-0.5 pr-3 text-zinc-600 text-[9px]">{k.group}</td>
                  <td className={`py-0.5 text-right tabular-nums ${k.lag >= 1000 ? "text-rose-400" : k.lag >= 100 ? "text-amber-400" : "text-zinc-600"}`}>{k.lag}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="flex gap-4 text-[10px]">
            <span>
              <span className="text-zinc-600">redis </span>
              <span className="text-zinc-400">{fmtBytes(siemPipeline.redis_memory_used_bytes)}/{fmtBytes(siemPipeline.redis_memory_max_bytes)}</span>
            </span>
            <span className="text-zinc-600">{siemPipeline.redis_ops_per_sec.toFixed(0)} ops/s</span>
          </div>
        </div>
      )}

      {/* HITL Queue */}
      <div className="px-4 py-3">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-[9px] text-zinc-600 uppercase tracking-wider">HITL Queue</span>
          {hitlItems.length > 0 && <span className="text-[8px] bg-rose-500/20 text-rose-400 px-1.5 rounded-full">{hitlItems.length}</span>}
        </div>
        {hitlItems.length === 0 ? (
          <p className="text-zinc-600">no pending approvals</p>
        ) : (
          <div className="space-y-1">
            {hitlItems.map((h) => {
              const dec = hitlDecisions[h.incident_id];
              const timeoutSec = Math.max(0, 900 - h.waiting_sec);
              return (
                <div key={h.incident_id} className="flex items-center gap-3 py-1.5 px-3 bg-zinc-900 border border-rose-500/20 rounded">
                  <div className="flex-1 min-w-0">
                    <span className="text-rose-300">{h.category}</span>
                    <span className="text-zinc-700 mx-1.5">·</span>
                    <span className="text-zinc-500">{h.severity}</span>
                    <span className="text-zinc-700 mx-1.5">·</span>
                    <span className="text-amber-400">{age(timeoutSec)} left</span>
                    <p className="text-[9px] text-zinc-600 font-mono truncate mt-0.5">{h.trace_id}</p>
                  </div>
                  {dec === undefined ? (
                    <div className="flex gap-1 shrink-0">
                      <button onClick={() => decideHitl(h.incident_id, h.trace_id, "approved")} className="px-2 py-0.5 bg-emerald-500/10 border border-emerald-500/30 hover:bg-emerald-500/20 text-emerald-400 text-[9px] rounded">APPROVE</button>
                      <button onClick={() => decideHitl(h.incident_id, h.trace_id, "rejected")} className="px-2 py-0.5 bg-rose-500/10 border border-rose-500/30 hover:bg-rose-500/20 text-rose-400 text-[9px] rounded">REJECT</button>
                    </div>
                  ) : dec === "pending" ? (
                    <span className="text-zinc-600 animate-pulse text-[9px]">…</span>
                  ) : dec === "approved" ? (
                    <span className="text-emerald-400 text-[9px]">APPROVED</span>
                  ) : dec === "rejected" ? (
                    <span className="text-rose-400 text-[9px]">REJECTED</span>
                  ) : (
                    <span className="text-amber-400 text-[9px]">ERR</span>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Alert Injection */}
      <div className="px-4 py-3">
        <p className="text-[9px] text-zinc-600 uppercase tracking-wider mb-2">Alert Injection</p>
        <div className="space-y-1.5">
          <div className="grid grid-cols-2 gap-1.5">
            {(["alertname", "namespace", "pod", "severity"] as const).map((field) => (
              <div key={field}>
                <label className="text-[9px] text-zinc-600 block mb-0.5">{field}</label>
                <input
                  value={alertForm[field]}
                  onChange={(e) => setAlertForm((f) => ({ ...f, [field]: e.target.value }))}
                  className="w-full bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-[10px] text-zinc-200 font-mono focus:outline-none focus:border-amber-500/50"
                />
              </div>
            ))}
          </div>
          <div>
            <label className="text-[9px] text-zinc-600 block mb-0.5">summary</label>
            <input
              value={alertForm.summary}
              onChange={(e) => setAlertForm((f) => ({ ...f, summary: e.target.value }))}
              className="w-full bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-[10px] text-zinc-200 font-mono focus:outline-none focus:border-amber-500/50"
            />
          </div>
          <button
            onClick={sendAlert}
            disabled={alertStatus === "sending"}
            className="flex items-center gap-2 px-3 py-1.5 bg-amber-500/10 border border-amber-500/30 hover:bg-amber-500/20 text-amber-400 text-[9px] rounded transition-colors disabled:opacity-50"
          >
            <Send size={9} />
            {alertStatus === "sending" ? "sending…" : alertStatus === "ok" ? "sent ✓" : alertStatus === "err" ? "error ✗" : "POST /webhook/prometheus"}
          </button>
          <p className="text-[9px] text-zinc-700">→ kafka:omni-alerts → prober → pipeline</p>
        </div>
      </div>

      {/* Telegram */}
      <div className="px-4 py-3">
        <p className="text-[9px] text-zinc-600 uppercase tracking-wider mb-1">Telegram</p>
        <div className="flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-zinc-600" />
          <span className="text-zinc-500">chat_id:-5174042122</span>
          <span className="text-zinc-700">·</span>
          <span className="text-zinc-600">push delivery only</span>
        </div>
      </div>

      <div className="px-4 py-4 text-center">
        <p className="text-zinc-700">← select incident for advisory</p>
      </div>
    </div>
  );
}
