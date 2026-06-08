"use client";

import { useCallback, useEffect, useState } from "react";
import { SectionLabel, Unavailable } from "@/components/shared/primitives";

// Tier Control — graduated autonomy (MASTER_PLAN §6). shadow → assist → auto.
// Operator-only tier change with readiness gauge + 2-step confirm on promotion.
// Never auto-jumps tier; this panel is the ONLY way to change it.

type Tier = "shadow" | "assist" | "auto";

const TIERS: Tier[] = ["shadow", "assist", "auto"];
const TIER_RANK: Record<Tier, number> = { shadow: 0, assist: 1, auto: 2 };
const TIER_DESC: Record<Tier, string> = {
  shadow: "read-only · điều tra + SUGGEST",
  assist: "tự chạy LOW · MEDIUM → HITL",
  auto: "tự chạy LOW+MEDIUM · HIGH → HITL",
};
const TIER_COLOR: Record<Tier, string> = {
  shadow: "text-sky-400 border-sky-500/40",
  assist: "text-amber-400 border-amber-500/40",
  auto: "text-rose-400 border-rose-500/40",
};

interface TierReadiness {
  current_tier: Tier;
  next_tier: Tier | null;
  ready: boolean;
  elapsed_days: number;
  accepted: number;
  rejected: number;
  false_positive: number;
  total: number;
  wilson_lb: number;
  false_positive_rate: number;
  reasons: string[];
}

interface TierControlPanelProps {
  tenant: string;
}

export function TierControlPanel({ tenant }: TierControlPanelProps) {
  const [tier, setTier] = useState<Tier | null>(null);
  const [readiness, setReadiness] = useState<TierReadiness | null>(null);
  const [loadErr, setLoadErr] = useState(false);
  const [pending, setPending] = useState<Tier | null>(null);
  const [status, setStatus] = useState<"idle" | "saving" | "ok" | "err">("idle");
  const [statusMsg, setStatusMsg] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api/autonomy?tenant_id=${encodeURIComponent(tenant)}`, { cache: "no-store" });
      if (!res.ok) {
        setLoadErr(true);
        return;
      }
      const data = (await res.json()) as { tier: Tier; readiness: TierReadiness | null };
      setTier(data.tier);
      setReadiness(data.readiness);
      setLoadErr(false);
    } catch {
      setLoadErr(true);
    }
  }, [tenant]);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 15_000);
    return () => clearInterval(t);
  }, [load]);

  const requestChange = useCallback((target: Tier) => {
    setStatus("idle");
    setStatusMsg("");
    setPending(target);
  }, []);

  const confirmChange = useCallback(async () => {
    if (!pending || !tier) return;
    const isPromotion = TIER_RANK[pending] > TIER_RANK[tier];
    setStatus("saving");
    try {
      const res = await fetch("/api/autonomy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tier: pending,
          tenant_id: tenant,
          actor: "admin_ui",
          confirm: isPromotion,
          forced: isPromotion && !(readiness?.ready ?? false),
        }),
      });
      const data = (await res.json().catch(() => ({}))) as { error?: string; detail?: string };
      if (res.ok) {
        setStatus("ok");
        setStatusMsg(`${tier} → ${pending}`);
        setPending(null);
        await load();
      } else {
        setStatus("err");
        setStatusMsg(data.error ?? data.detail ?? `gateway ${res.status}`);
      }
    } catch {
      setStatus("err");
      setStatusMsg("network error");
    }
    setTimeout(() => setStatus("idle"), 4000);
  }, [pending, tier, tenant, readiness, load]);

  if (loadErr && tier === null) {
    return (
      <div>
        <SectionLabel text="A · Autonomy Tier" />
        <Unavailable detail="tier unavailable (gateway /autonomy/tier · OMNI_ADMIN_PG_DSN?)" />
      </div>
    );
  }

  const isPromotion = pending && tier ? TIER_RANK[pending] > TIER_RANK[tier] : false;
  const wilsonPct = readiness ? Math.round(readiness.wilson_lb * 100) : 0;

  return (
    <div>
      <SectionLabel text="A · Autonomy Tier" note={<span className="text-zinc-600">operator-only · không tự nhảy</span>} />

      {/* Tier selector row */}
      <div className="flex gap-2 mb-3">
        {TIERS.map((t) => {
          const active = t === tier;
          return (
            <button
              key={t}
              type="button"
              onClick={() => requestChange(t)}
              disabled={active}
              className={`flex-1 rounded border px-2 py-1.5 text-left transition ${
                active
                  ? `${TIER_COLOR[t]} bg-zinc-900/60`
                  : "border-zinc-800 text-zinc-500 hover:border-zinc-700 hover:bg-zinc-900/40"
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-semibold uppercase tracking-wider">{t}</span>
                {active && <span className="text-[8px] text-emerald-400">● active</span>}
              </div>
              <div className="text-[9px] text-zinc-600 mt-0.5">{TIER_DESC[t]}</div>
            </button>
          );
        })}
      </div>

      {/* Readiness gauge */}
      {readiness && readiness.next_tier && (
        <div className="rounded border border-zinc-800 p-2 mb-2 text-[9px]">
          <div className="flex items-center justify-between mb-1">
            <span className="text-zinc-500">
              readiness {readiness.current_tier} → {readiness.next_tier}
            </span>
            <span className={readiness.ready ? "text-emerald-400" : "text-amber-400"}>
              {readiness.ready ? "READY" : "not ready"}
            </span>
          </div>
          <div className="h-1.5 rounded-full bg-zinc-800 overflow-hidden mb-1">
            <div
              className={`h-full ${readiness.ready ? "bg-emerald-500" : "bg-amber-500"}`}
              style={{ width: `${Math.min(100, wilsonPct)}%` }}
            />
          </div>
          <div className="grid grid-cols-4 gap-1 text-zinc-600 tabular-nums">
            <span>wilson {readiness.wilson_lb.toFixed(2)}</span>
            <span>adv {readiness.total}</span>
            <span>fp {(readiness.false_positive_rate * 100).toFixed(0)}%</span>
            <span>{readiness.elapsed_days}d</span>
          </div>
          {readiness.reasons.length > 0 && (
            <div className="mt-1 text-amber-500/70">⚠ {readiness.reasons.join(" · ")}</div>
          )}
        </div>
      )}

      {/* 2-step confirm */}
      {pending && (
        <div className={`rounded border p-2 text-[10px] ${isPromotion ? "border-rose-500/50 bg-rose-950/20" : "border-zinc-700 bg-zinc-900/40"}`}>
          <div className="text-zinc-300 mb-1.5">
            {isPromotion ? "⬆ NÂNG tier" : "⬇ Hạ tier"}: <span className="font-semibold">{tier}</span> → <span className="font-semibold">{pending}</span>
            {isPromotion && !readiness?.ready && <span className="text-amber-400"> (forced — readiness chưa đạt)</span>}
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={confirmChange}
              disabled={status === "saving"}
              className={`rounded px-2.5 py-1 text-[10px] font-semibold ${isPromotion ? "bg-rose-600 hover:bg-rose-500" : "bg-zinc-700 hover:bg-zinc-600"} text-white disabled:opacity-50`}
            >
              {status === "saving" ? "…" : isPromotion ? "Xác nhận nâng (2-step)" : "Xác nhận"}
            </button>
            <button
              type="button"
              onClick={() => setPending(null)}
              className="rounded px-2.5 py-1 text-[10px] text-zinc-400 hover:text-zinc-200"
            >
              Huỷ
            </button>
          </div>
        </div>
      )}

      {status === "ok" && <div className="mt-1.5 text-[9px] text-emerald-400">✓ {statusMsg}</div>}
      {status === "err" && <div className="mt-1.5 text-[9px] text-rose-400">✗ {statusMsg}</div>}
    </div>
  );
}
