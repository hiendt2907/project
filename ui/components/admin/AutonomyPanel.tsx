import { useCallback, useState } from "react";
import { SectionLabel, Unavailable } from "@/components/shared/primitives";
import { LANES, LANE_LABEL } from "@/components/shared/lane-tokens";
import type { AutonomyPolicyResponse, AutonomyLevel } from "@/app/api/config/autonomy/route";
import { SEVERITIES, LEVEL_SHORT, LEVEL_COLOR } from "./types";

const AUTONOMY_LEVELS: AutonomyLevel[] = ["FULL_AUTO", "SUGGEST_ONLY", "HITL", "ALERT_ONLY"];

interface AutonomyPanelProps {
  autonomy: AutonomyPolicyResponse | null;
  error?: boolean;
  onSaved: () => void;
}

export function AutonomyPanel({ autonomy, error, onSaved }: AutonomyPanelProps) {
  const [ruleForm, setRuleForm] = useState<{ lane: string; severity: string; level: AutonomyLevel }>({
    lane: "SYS_RESOURCE",
    severity: "critical",
    level: "HITL",
  });
  const [ruleStatus, setRuleStatus] = useState<"idle" | "saving" | "ok" | "err">("idle");

  const saveRule = useCallback(async () => {
    setRuleStatus("saving");
    try {
      const res = await fetch("/api/config/autonomy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(ruleForm),
      });
      setRuleStatus(res.ok ? "ok" : "err");
      if (res.ok) onSaved();
    } catch {
      setRuleStatus("err");
    }
    setTimeout(() => setRuleStatus("idle"), 3000);
  }, [ruleForm, onSaved]);

  function autonomyLevel(lane: string, sev: string): string {
    if (!autonomy) return "—";
    return autonomy.rules.find((r) => r.lane === lane && r.severity === sev)?.level ?? "—";
  }

  if (error && !autonomy) {
    return (
      <div>
        <SectionLabel
        text="C · Autonomy Policy (legacy per-lane)"
        note={
          <span className="text-amber-500/70" title="Lưu Redis, không phải Postgres omni_admin">
            ⚠ Redis-only · Tier ở trên mới là source-of-truth
          </span>
        }
      />
        <Unavailable detail="policy unavailable (gateway /autonomy/policy)" />
      </div>
    );
  }
  return (
    <div>
      <SectionLabel
        text="C · Autonomy Policy (legacy per-lane)"
        note={
          <span className="text-amber-500/70" title="Lưu Redis, không phải Postgres omni_admin">
            ⚠ Redis-only · Tier ở trên mới là source-of-truth
          </span>
        }
      />
      <table className="w-full text-[10px] border-collapse">
        <thead>
          <tr>
            <th className="text-left pb-1 pr-4 text-zinc-600 font-normal w-20">lane</th>
            {SEVERITIES.map((s) => (
              <th key={s} className="text-center pb-1 px-1 text-zinc-600 font-normal text-[9px]">{s}</th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-800/30">
          {LANES.map((lane) => (
            <tr key={lane} className="hover:bg-zinc-900/40">
              <td className="py-1 pr-4 text-zinc-500 text-[9px]">{LANE_LABEL[lane]}</td>
              {SEVERITIES.map((sev) => {
                const lv = autonomyLevel(lane, sev);
                return (
                  <td key={sev} className={`py-1 px-1 text-center text-[9px] tabular-nums ${LEVEL_COLOR[lv] ?? "text-zinc-600"}`}>
                    {lv === "—" ? "—" : (LEVEL_SHORT[lv] ?? lv)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>

      <div className="mt-3 pt-2 border-t border-zinc-800/40">
        <p className="text-[9px] text-zinc-600 mb-2 uppercase tracking-wider">Update Rule</p>
        <div className="flex items-center gap-2 flex-wrap">
          <select
            value={ruleForm.lane}
            onChange={(e) => setRuleForm((f) => ({ ...f, lane: e.target.value }))}
            className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-[10px] text-zinc-300 focus:outline-none focus:border-amber-500/50"
          >
            {LANES.map((l) => <option key={l} value={l}>{LANE_LABEL[l]}</option>)}
          </select>
          <select
            value={ruleForm.severity}
            onChange={(e) => setRuleForm((f) => ({ ...f, severity: e.target.value }))}
            className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-[10px] text-zinc-300 focus:outline-none focus:border-amber-500/50"
          >
            {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <select
            value={ruleForm.level}
            onChange={(e) => setRuleForm((f) => ({ ...f, level: e.target.value as AutonomyLevel }))}
            className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-[10px] text-zinc-300 focus:outline-none focus:border-amber-500/50"
          >
            {AUTONOMY_LEVELS.map((lv) => <option key={lv} value={lv}>{LEVEL_SHORT[lv]}</option>)}
          </select>
          <button
            onClick={() => void saveRule()}
            disabled={ruleStatus === "saving"}
            className="px-2.5 py-1 bg-amber-500/10 border border-amber-500/30 hover:bg-amber-500/20 text-amber-400 text-[9px] rounded transition-colors disabled:opacity-50"
          >
            {ruleStatus === "saving" ? "…" : ruleStatus === "ok" ? "saved ✓" : ruleStatus === "err" ? "error ✗" : "save"}
          </button>
        </div>
      </div>

      {autonomy?.history && autonomy.history.length > 0 && (
        <div className="mt-2 pt-2 border-t border-zinc-800/40">
          <p className="text-[9px] text-zinc-600 mb-1">recent changes</p>
          {autonomy.history.slice(0, 3).map((h) => (
            <div key={h.id} className="flex items-center gap-2 text-[9px] py-0.5">
              <span className="text-zinc-600">{LANE_LABEL[h.lane] ?? h.lane}/{h.severity}</span>
              <span className="text-zinc-700">→</span>
              <span className={LEVEL_COLOR[h.new_level] ?? "text-zinc-500"}>{LEVEL_SHORT[h.new_level] ?? h.new_level}</span>
              <span className="text-zinc-700 ml-auto">{h.operator}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
