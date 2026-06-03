import { SectionLabel, Unavailable } from "@/components/shared/primitives";
import { pct } from "@/components/shared/fmt";
import { LANE_LABEL } from "@/components/shared/lane-tokens";
import type { KpiSummary } from "./types";

export function KpiPanel({ kpi, error }: { kpi: KpiSummary | null; error?: boolean }) {
  if (error && !kpi) {
    return (
      <div>
        <SectionLabel text="B · KPI 24h" />
        <Unavailable detail="KPI unavailable (gateway /kpi)" />
      </div>
    );
  }
  return (
    <div>
      <SectionLabel text="B · KPI 24h" />
      {!kpi ? (
        <div className="text-[10px] text-zinc-600 animate-pulse">loading…</div>
      ) : (
        <>
          <div className="flex items-end gap-6 mb-3">
            <div>
              <div className={`text-2xl font-bold tabular-nums leading-none ${kpi.acceptance_rate != null ? (kpi.acceptance_rate >= 0.8 ? "text-emerald-400" : kpi.acceptance_rate >= 0.6 ? "text-amber-400" : "text-rose-400") : "text-zinc-700"}`}>
                {pct(kpi.acceptance_rate)}
              </div>
              <p className="text-[9px] text-zinc-600 mt-0.5">acceptance · {kpi.accepted}/{kpi.total}</p>
            </div>
            <div>
              <div className={`text-2xl font-bold tabular-nums leading-none ${kpi.false_positive_rate != null && kpi.false_positive_rate > 0.1 ? "text-rose-400" : kpi.false_positive_rate != null && kpi.false_positive_rate > 0.05 ? "text-amber-400" : "text-zinc-300"}`}>
                {pct(kpi.false_positive_rate)}
              </div>
              <p className="text-[9px] text-zinc-600 mt-0.5">false positive · {kpi.fp_count} cases</p>
            </div>
          </div>
          <table className="w-full text-[10px] border-collapse">
            <thead>
              <tr>
                <th className="text-left pb-1 pr-4 text-zinc-600 font-normal text-[9px]">lane</th>
                <th className="text-right pb-1 pr-4 text-zinc-600 font-normal text-[9px]">detected</th>
                <th className="text-right pb-1 pr-4 text-zinc-600 font-normal text-[9px]">resolved</th>
                <th className="text-right pb-1 text-zinc-600 font-normal text-[9px]">res%</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/30">
              {kpi.trend.map((t) => {
                const rate = t.detected > 0 ? t.resolved / t.detected : null;
                return (
                  <tr key={t.lane} className="hover:bg-zinc-900/40">
                    <td className="py-1 pr-4 text-zinc-500 text-[9px]">{LANE_LABEL[t.lane] ?? t.lane}</td>
                    <td className="py-1 pr-4 text-right tabular-nums text-zinc-400">{t.detected}</td>
                    <td className="py-1 pr-4 text-right tabular-nums text-zinc-400">{t.resolved}</td>
                    <td className={`py-1 text-right tabular-nums ${rate != null ? (rate >= 0.9 ? "text-emerald-400" : rate >= 0.7 ? "text-amber-400" : "text-rose-400") : "text-zinc-700"}`}>
                      {rate != null ? `${(rate * 100).toFixed(1)}%` : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
