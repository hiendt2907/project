import { SectionLabel, Unavailable } from "@/components/shared/primitives";
import { pct, fmtK } from "@/components/shared/fmt";
import type { SiemTelemetry } from "./types";

export function LlmRagPanel({ siem, error }: { siem: SiemTelemetry | null; error?: boolean }) {
  if (error && !siem) {
    return (
      <div>
        <SectionLabel text="E · LLM & RAG" />
        <Unavailable detail="LLM/RAG telemetry unavailable (SIEM_METRICS_URL)" />
      </div>
    );
  }
  return (
    <div>
      <SectionLabel text="E · LLM & RAG" />
      {!siem ? (
        <div className="text-[10px] text-zinc-600 animate-pulse">loading…</div>
      ) : (
        <>
          <div className="mb-3">
            <p className="text-[9px] text-zinc-700 uppercase tracking-wider mb-1.5">LLM</p>
            <div className="grid grid-cols-3 gap-x-4 gap-y-1 text-[10px]">
              <div><span className="text-zinc-600">calls </span><span className="text-zinc-300">{fmtK(siem.llm.total_calls_24h)}/24h</span></div>
              <div>
                <span className="text-zinc-600">success </span>
                <span className={siem.llm.success_rate !== null && siem.llm.success_rate < 0.95 ? "text-amber-400" : "text-emerald-400"}>{pct(siem.llm.success_rate)}</span>
              </div>
              <div><span className="text-zinc-600">p50 </span><span className="text-zinc-300">{siem.llm.latency_p50_ms !== null ? `${(siem.llm.latency_p50_ms / 1000).toFixed(1)}s` : "—"}</span></div>
              <div>
                <span className="text-zinc-600">p95 </span>
                <span className={siem.llm.latency_p95_ms !== null && siem.llm.latency_p95_ms > 5000 ? "text-amber-400" : "text-zinc-300"}>{siem.llm.latency_p95_ms !== null ? `${(siem.llm.latency_p95_ms / 1000).toFixed(1)}s` : "—"}</span>
              </div>
              <div><span className="text-zinc-600">tok in </span><span className="text-zinc-300">{fmtK(siem.llm.tokens_in_total)}</span></div>
              <div><span className="text-zinc-600">tok out </span><span className="text-zinc-300">{fmtK(siem.llm.tokens_out_total)}</span></div>
            </div>
          </div>
          <div className="pt-2 border-t border-zinc-800/40">
            <p className="text-[9px] text-zinc-700 uppercase tracking-wider mb-1.5">RAG</p>
            <div className="grid grid-cols-3 gap-x-4 gap-y-1 text-[10px]">
              <div><span className="text-zinc-600">queries </span><span className="text-zinc-300">{fmtK(siem.rag.queries_24h)}/24h</span></div>
              <div>
                <span className="text-zinc-600">fastpath </span>
                <span className={siem.rag.cache_hit_ratio === null ? "text-zinc-500" : siem.rag.cache_hit_ratio >= 0.7 ? "text-emerald-400" : siem.rag.cache_hit_ratio >= 0.4 ? "text-amber-400" : "text-rose-400"}>{pct(siem.rag.cache_hit_ratio)}</span>
              </div>
              <div><span className="text-zinc-600">latency </span><span className="text-zinc-300">{siem.rag.avg_query_latency_ms !== null ? `${siem.rag.avg_query_latency_ms.toFixed(0)}ms` : "—"}</span></div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
