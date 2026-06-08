"use client";

import { useCallback, useEffect, useState } from "react";
import { SectionLabel, Unavailable } from "@/components/shared/primitives";

// HITL Queue — approve/reject pending mutations on UI in parallel with Telegram
// (MASTER_PLAN §4/§6.7). CRAT HITL_DECISION enqueued before dispatch (fail-closed).

interface HitlPending {
  pending_id: string;
  tool_name: string;
  risk_class: string;
  tier_at_time: string;
  decision: string;
  channel: string;
  created_at: string | null;
}

interface HitlQueuePanelProps {
  tenant: string;
}

const RISK_COLOR: Record<string, string> = {
  LOW: "text-emerald-400",
  MEDIUM: "text-amber-400",
  HIGH: "text-rose-400",
};

export function HitlQueuePanel({ tenant }: HitlQueuePanelProps) {
  const [pending, setPending] = useState<HitlPending[] | null>(null);
  const [loadErr, setLoadErr] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api/autonomy/hitl?tenant_id=${encodeURIComponent(tenant)}`, { cache: "no-store" });
      if (!res.ok) return setLoadErr(true);
      const data = (await res.json()) as { pending: HitlPending[] };
      setPending(data.pending);
      setLoadErr(false);
    } catch {
      setLoadErr(true);
    }
  }, [tenant]);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 10_000);
    return () => clearInterval(t);
  }, [load]);

  const decide = useCallback(
    async (pendingId: string, decision: "APPROVED" | "REJECTED") => {
      setBusy(pendingId);
      setMsg("");
      try {
        const res = await fetch(`/api/autonomy/hitl/${encodeURIComponent(pendingId)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision, tenant_id: tenant, actor: "admin_ui" }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) setMsg(`✗ ${data.detail ?? data.error ?? res.status}`);
        else {
          setMsg(`✓ ${pendingId.slice(0, 12)}… ${decision}`);
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

  if (loadErr) {
    return (
      <div>
        <SectionLabel text="HITL Queue" />
        <Unavailable detail="gateway /autonomy/hitl unreachable" />
      </div>
    );
  }

  return (
    <div>
      <SectionLabel
        text="HITL Queue"
        note={<span className="text-zinc-600">duyệt song song Telegram · CRAT trước dispatch</span>}
      />
      <div className="border border-zinc-800 bg-zinc-900/40 min-h-[3rem]">
        {pending === null ? (
          <div className="p-3 text-zinc-600 text-[10px]">loading…</div>
        ) : pending.length === 0 ? (
          <div className="p-3 text-zinc-600 text-[10px]">no pending approvals</div>
        ) : (
          pending.map((p) => (
            <div key={p.pending_id} className="flex items-center justify-between px-2 py-1.5 border-b border-zinc-800/60 text-[10px]">
              <div className="flex flex-col">
                <span className="text-zinc-200">
                  <code>{p.tool_name}</code>
                  <span className={`ml-2 ${RISK_COLOR[p.risk_class] ?? "text-zinc-500"}`}>{p.risk_class}</span>
                  <span className="ml-2 text-zinc-600">tier:{p.tier_at_time}</span>
                </span>
                <span className="text-zinc-700">{p.pending_id.slice(0, 20)}… · {p.created_at?.slice(11, 19)}</span>
              </div>
              <div className="flex items-center gap-1">
                <button
                  disabled={busy === p.pending_id}
                  onClick={() => decide(p.pending_id, "APPROVED")}
                  className="px-2 py-0.5 border border-emerald-500/40 text-emerald-400 text-[9px] hover:bg-emerald-500/10 disabled:opacity-40"
                >
                  ✅ approve
                </button>
                <button
                  disabled={busy === p.pending_id}
                  onClick={() => decide(p.pending_id, "REJECTED")}
                  className="px-2 py-0.5 border border-rose-500/40 text-rose-400 text-[9px] hover:bg-rose-500/10 disabled:opacity-40"
                >
                  ❌ reject
                </button>
              </div>
            </div>
          ))
        )}
        {msg && <div className="px-2 py-1 border-t border-zinc-800 text-[10px] text-zinc-400">{msg}</div>}
      </div>
    </div>
  );
}
