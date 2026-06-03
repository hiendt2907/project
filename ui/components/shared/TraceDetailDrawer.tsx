"use client";

// Slide-over drawer hosting the T3 TraceSessionView. Used by the admin dashboard
// when a CRAT block / trace is clicked.

import { TraceSessionView } from "@/components/shared/TraceSessionView";

interface TraceDetailDrawerProps {
  traceId: string | null;
  onClose: () => void;
}

export function TraceDetailDrawer({ traceId, onClose }: TraceDetailDrawerProps) {
  if (!traceId) return null;
  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <button
        aria-label="Close session detail"
        onClick={onClose}
        className="absolute inset-0 bg-black/60 backdrop-blur-[1px]"
      />
      <aside className="relative w-full max-w-md h-full bg-zinc-950 border-l border-zinc-800 overflow-y-auto shadow-2xl">
        <div className="sticky top-0 z-10 flex items-center justify-between px-4 h-9 border-b border-zinc-800 bg-zinc-950">
          <span className="text-[10px] text-amber-400 uppercase tracking-widest font-semibold">Diagnosis Session</span>
          <div className="flex items-center gap-2">
            <code className="text-[9px] text-zinc-600 font-mono">{traceId.slice(0, 18)}</code>
            <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300 text-xs">✕</button>
          </div>
        </div>
        <div className="p-4">
          <TraceSessionView traceId={traceId} />
        </div>
      </aside>
    </div>
  );
}
