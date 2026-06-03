import { useCallback } from "react";
import { SectionLabel, Unavailable } from "@/components/shared/primitives";
import { age } from "@/components/shared/fmt";
import type { CratResponse } from "@/app/api/crat/route";
import { EVENT_COLOR } from "./types";

interface CratPanelProps {
  crat: CratResponse | null;
  now: number;
  error?: boolean;
  onSelectTrace: (traceId: string) => void;
}

export function CratPanel({ crat, now, error, onSelectTrace }: CratPanelProps) {
  const exportCrat = useCallback(() => {
    if (!crat?.blocks) return;
    const data = JSON.stringify(crat.blocks, null, 2);
    const url = URL.createObjectURL(new Blob([data], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `crat-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, [crat]);

  return (
    <div>
      <SectionLabel
        text={`D · CRAT${crat?.total ? ` · ${crat.total}/24h` : ""}`}
        note={
          <div className="flex items-center gap-1.5">
            {crat && (
              <button onClick={exportCrat} className="text-[8px] text-zinc-500 border border-zinc-700 px-1.5 py-0.5 rounded hover:bg-zinc-800 transition-colors">
                export json
              </button>
            )}
          </div>
        }
      />
      {error && !crat ? (
        <Unavailable detail="CRAT chain unavailable (gateway /crat)" />
      ) : !crat ? (
        <div className="text-[10px] text-zinc-600 animate-pulse">loading…</div>
      ) : !crat.blocks || crat.blocks.length === 0 ? (
        <div className="text-[10px] text-zinc-600">no blocks in last 24h</div>
      ) : (
        <div className="divide-y divide-zinc-800/30">
          {crat.blocks.map((b) => {
            const ageSec = b.timestamp ? Math.floor((now - new Date(b.timestamp).getTime()) / 1000) : 0;
            const hasTrace = !!b.trace_id;
            return (
              <button
                key={b.seq}
                onClick={() => hasTrace && onSelectTrace(b.trace_id)}
                disabled={!hasTrace}
                title={hasTrace ? "open diagnosis session (T3)" : undefined}
                className={`w-full py-1 flex items-center gap-2 text-[10px] text-left ${hasTrace ? "hover:bg-zinc-900/50 cursor-pointer" : "cursor-default"}`}
              >
                <span className="text-zinc-700 w-8 tabular-nums shrink-0">#{b.seq}</span>
                <span className={`flex-1 truncate ${EVENT_COLOR[b.event_type] ?? "text-zinc-400"}`}>{b.event_type}</span>
                <span className="text-zinc-600 font-mono hidden sm:block shrink-0">{b.trace_id?.slice(0, 8)}</span>
                {b.has_signature && <span className="text-emerald-400 shrink-0 text-[9px]">sig</span>}
                {hasTrace && <span className="text-amber-500/60 shrink-0 text-[9px]">↗</span>}
                <span className="text-zinc-600 shrink-0 w-10 text-right">{age(ageSec)}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
