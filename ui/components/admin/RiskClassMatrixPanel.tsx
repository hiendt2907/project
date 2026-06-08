"use client";

import { useCallback, useEffect, useState } from "react";
import { SectionLabel, Unavailable } from "@/components/shared/primitives";

// Risk-Class Matrix — tool × risk_class with click-to-override (MASTER_PLAN §2/§6.7).
// dangerous_tools locked to HIGH; downgrading risk needs 2-step confirm.

type RiskClass = "READONLY" | "LOW" | "MEDIUM" | "HIGH";
const CLASSES: RiskClass[] = ["READONLY", "LOW", "MEDIUM", "HIGH"];
const RANK: Record<RiskClass, number> = { READONLY: 0, LOW: 1, MEDIUM: 2, HIGH: 3 };
const CLASS_COLOR: Record<RiskClass, string> = {
  READONLY: "text-zinc-400 border-zinc-600",
  LOW: "text-emerald-400 border-emerald-500/40",
  MEDIUM: "text-amber-400 border-amber-500/40",
  HIGH: "text-rose-400 border-rose-500/40",
};

interface ToolRow {
  tool_name: string;
  static_risk_class: RiskClass;
  override: RiskClass | null;
  effective: RiskClass;
  dangerous_locked: boolean;
  reason: string | null;
}

interface RiskClassMatrixPanelProps {
  tenant: string;
}

export function RiskClassMatrixPanel({ tenant }: RiskClassMatrixPanelProps) {
  const [tools, setTools] = useState<ToolRow[] | null>(null);
  const [loadErr, setLoadErr] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState("");
  const [filter, setFilter] = useState("");
  // Explicit "Assign / Override" form state.
  const [formTool, setFormTool] = useState("");
  const [formClass, setFormClass] = useState<RiskClass>("READONLY");
  const [formReason, setFormReason] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api/autonomy/risk-class?tenant_id=${encodeURIComponent(tenant)}`, { cache: "no-store" });
      if (!res.ok) return setLoadErr(true);
      const data = (await res.json()) as { tools: ToolRow[] };
      setTools(data.tools);
      setLoadErr(false);
    } catch {
      setLoadErr(true);
    }
  }, [tenant]);

  useEffect(() => {
    void load();
  }, [load]);

  // Single write path shared by the explicit form and the inline R/L/M/H cells.
  const submitOverride = useCallback(
    async (toolName: string, target: RiskClass, staticClass: RiskClass, reason: string | null) => {
      const isDowngrade = RANK[target] < RANK[staticClass];
      if (isDowngrade) {
        const ok = window.confirm(
          `Hạ rủi ro ${toolName}: ${staticClass} → ${target}?\nXác nhận 2 bước (tăng quyền tự chạy).`,
        );
        if (!ok) return;
      }
      setBusy(toolName);
      setMsg("");
      try {
        const res = await fetch("/api/autonomy/risk-class", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tool_name: toolName,
            risk_class: target,
            reason: reason || null,
            tenant_id: tenant,
            actor: "admin_ui",
            confirm: isDowngrade,
          }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          setMsg(`✗ ${data.detail ?? data.error ?? res.status}`);
        } else {
          setMsg(`✓ ${toolName} → ${target}`);
          await load();
        }
      } catch {
        setMsg("✗ network error");
      } finally {
        setBusy(null);
      }
    },
    [tenant, load],
  );

  const apply = useCallback(
    async (row: ToolRow, target: RiskClass) => {
      if (row.dangerous_locked) return;
      if (target === row.effective) return;
      await submitOverride(row.tool_name, target, row.static_risk_class, null);
    },
    [submitOverride],
  );

  if (loadErr) {
    return (
      <div>
        <SectionLabel text="Risk-Class Matrix" />
        <Unavailable detail="gateway /autonomy/risk-class unreachable" />
      </div>
    );
  }

  const shown = (tools ?? []).filter((t) => t.tool_name.includes(filter));
  const formRow = (tools ?? []).find((t) => t.tool_name === formTool);

  const submitForm = async () => {
    if (!formRow) {
      setMsg("✗ chọn một tool");
      return;
    }
    if (formRow.dangerous_locked) {
      setMsg(`✗ ${formTool} là dangerous tool — khoá HIGH`);
      return;
    }
    await submitOverride(formTool, formClass, formRow.static_risk_class, formReason);
    setFormReason("");
  };

  return (
    <div>
      <SectionLabel
        text="Risk-Class Matrix"
        note={<span className="text-zinc-600">taxonomy cố định READONLY→HIGH · gán mức cho từng tool</span>}
      />

      {/* Explicit assign/override form — đường rõ ràng để "thêm" risk class cho 1 tool. */}
      <div className="mb-2 rounded border border-zinc-800 bg-zinc-900/60 p-2.5">
        <p className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
          Assign / Override risk class
        </p>
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col gap-0.5">
            <span className="text-[9px] text-zinc-600">tool</span>
            <select
              value={formTool}
              onChange={(e) => setFormTool(e.target.value)}
              className="w-48 bg-zinc-950 border border-zinc-700 px-2 py-1 text-[10px] text-zinc-200 outline-none focus:border-amber-500/50"
            >
              <option value="">— chọn tool —</option>
              {(tools ?? []).map((t) => (
                <option key={t.tool_name} value={t.tool_name}>
                  {t.tool_name}
                  {t.dangerous_locked ? " 🔒" : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="text-[9px] text-zinc-600">risk class</span>
            <select
              value={formClass}
              onChange={(e) => setFormClass(e.target.value as RiskClass)}
              className="bg-zinc-950 border border-zinc-700 px-2 py-1 text-[10px] text-zinc-200 outline-none focus:border-amber-500/50"
            >
              {CLASSES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-1 flex-col gap-0.5">
            <span className="text-[9px] text-zinc-600">reason (audit)</span>
            <input
              value={formReason}
              onChange={(e) => setFormReason(e.target.value)}
              placeholder="vì sao đổi mức rủi ro…"
              className="min-w-32 bg-zinc-950 border border-zinc-700 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-amber-500/50"
            />
          </label>
          <button
            disabled={!formTool || busy !== null}
            onClick={() => void submitForm()}
            className="border border-amber-500/40 bg-amber-500/10 px-3 py-1 text-[10px] font-semibold text-amber-300 transition-colors hover:bg-amber-500/20 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? "…" : "Apply"}
          </button>
        </div>
        {formRow && (
          <p className="mt-1.5 text-[9px] text-zinc-600">
            mặc định tĩnh: <span className="text-zinc-400">{formRow.static_risk_class}</span>
            {RANK[formClass] < RANK[formRow.static_risk_class] && (
              <span className="ml-2 text-rose-400">⚠ hạ rủi ro — cần xác nhận 2 bước</span>
            )}
          </p>
        )}
      </div>

      <div className="border border-zinc-800 bg-zinc-900/40">
        <div className="flex items-center justify-between px-2 py-1.5 border-b border-zinc-800">
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="filter tool…"
            className="bg-zinc-950 border border-zinc-700 px-2 py-0.5 text-[10px] text-zinc-300 w-40 outline-none focus:border-amber-500/50"
          />
          <span className="text-[10px] text-zinc-600">{shown.length} tools</span>
        </div>
        <div className="max-h-80 overflow-auto">
          {tools === null ? (
            <div className="p-3 text-zinc-600 text-[10px]">loading…</div>
          ) : (
            <table className="w-full text-[10px]">
              <tbody>
                {shown.map((row) => (
                  <tr key={row.tool_name} className="border-b border-zinc-800/60">
                    <td className="px-2 py-1 text-zinc-300 whitespace-nowrap">
                      {row.tool_name}
                      {row.dangerous_locked && <span className="ml-1 text-rose-500" title="dangerous — locked HIGH">🔒</span>}
                      {row.override && <span className="ml-1 text-amber-500" title="overridden">●</span>}
                    </td>
                    <td className="px-2 py-1">
                      <div className="flex gap-1 justify-end">
                        {CLASSES.map((c) => {
                          const active = row.effective === c;
                          const disabled = row.dangerous_locked || busy === row.tool_name;
                          return (
                            <button
                              key={c}
                              disabled={disabled}
                              onClick={() => apply(row, c)}
                              className={`px-1.5 py-0.5 border text-[9px] tracking-wide transition-colors ${
                                active ? CLASS_COLOR[c] + " bg-zinc-800" : "text-zinc-600 border-zinc-800 hover:border-zinc-600"
                              } ${disabled ? "opacity-40 cursor-not-allowed" : "cursor-pointer"}`}
                            >
                              {c[0]}
                            </button>
                          );
                        })}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        {msg && <div className="px-2 py-1 border-t border-zinc-800 text-[10px] text-zinc-400">{msg}</div>}
      </div>
    </div>
  );
}
