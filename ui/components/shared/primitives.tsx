// Shared presentational primitives — SectionLabel + StatCard.
// Previously redefined inline in the admin page.

import type { ReactNode } from "react";

export function SectionLabel({ text, note }: { text: string; note?: ReactNode }) {
  return (
    <div className="flex items-center gap-2 mb-2">
      <span className="text-[9px] text-zinc-500 uppercase tracking-wider font-medium">{text}</span>
      {note}
      <div className="flex-1 h-px bg-zinc-800" />
    </div>
  );
}

export function MockBadge() {
  return <span className="text-[8px] text-zinc-600 border border-zinc-700 px-1 rounded">mock</span>;
}

export function Loading() {
  return <span className="text-[9px] text-zinc-600 animate-pulse">loading…</span>;
}

export function ErrorBadge() {
  return <span className="text-[8px] text-rose-400 border border-rose-500/30 px-1 rounded">gateway error</span>;
}

export function Unavailable({ detail }: { detail?: string }) {
  return (
    <div className="text-[10px] text-rose-400/80 flex items-center gap-1.5">
      <span>✕</span>
      <span>{detail ?? "gateway unreachable — no data"}</span>
    </div>
  );
}

interface StatCardProps {
  label: string;
  value: string;
  color: string;
  sub: string;
}

export function StatCard({ label, value, color, sub }: StatCardProps) {
  return (
    <div className="flex-1 px-4 py-2.5 border-r border-zinc-800 last:border-r-0">
      <p className="text-[8px] text-zinc-600 uppercase tracking-wider mb-0.5">{label}</p>
      <p className={`text-base font-bold tabular-nums leading-none ${color}`}>{value}</p>
      <p className="text-[9px] text-zinc-700 mt-0.5">{sub}</p>
    </div>
  );
}
