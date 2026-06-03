import { SectionLabel, Loading, Unavailable } from "@/components/shared/primitives";
import { fmtBytes } from "@/components/shared/fmt";
import type { PodInfo, SiemTelemetry } from "./types";

interface WorkersPanelProps {
  pods: PodInfo[] | null;
  siem: SiemTelemetry | null;
  error?: boolean;
}

export function WorkersPanel({ pods, siem, error }: WorkersPanelProps) {
  const redisUsed = siem?.pipeline.redis_memory_used_bytes ?? null;
  const redisMax = siem?.pipeline.redis_memory_max_bytes ?? null;
  const redisPct = redisUsed !== null && redisMax !== null && redisMax > 0 ? redisUsed / redisMax : 0;
  if (error && pods === null) {
    return (
      <div>
        <SectionLabel text="A · Workers" />
        <Unavailable detail="worker status unavailable (gateway /agents)" />
      </div>
    );
  }
  return (
    <div>
      <SectionLabel text="A · Workers" note={pods === null ? <Loading /> : undefined} />
      <table className="w-full text-[10px] border-collapse">
        <thead>
          <tr>
            <th className="text-left pb-1 pr-4 text-zinc-600 font-normal">role</th>
            <th className="text-left pb-1 pr-4 text-zinc-600 font-normal">status</th>
            <th className="text-left pb-1 pr-4 text-zinc-600 font-normal">ready</th>
            <th className="text-left pb-1 pr-4 text-zinc-600 font-normal">hb</th>
            <th className="text-right pb-1 text-zinc-600 font-normal">err/24h</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-800/30">
          {(pods ?? []).map((pod) => (
            <tr key={pod.name} className="hover:bg-zinc-900/40">
              <td className="py-1 pr-4 text-zinc-200">{pod.name}</td>
              <td className={`py-1 pr-4 ${pod.status === "healthy" ? "text-emerald-400" : pod.status === "degraded" ? "text-amber-400" : "text-rose-400"}`}>
                {pod.status === "healthy" ? "● ok" : pod.status === "degraded" ? "▲ warn" : "✕ down"}
              </td>
              <td className="py-1 pr-4 text-zinc-400">{pod.ready}</td>
              <td className="py-1 pr-4 text-zinc-500">{pod.hb}</td>
              <td className={`py-1 text-right tabular-nums ${pod.error_count ? "text-rose-400" : "text-zinc-700"}`}>
                {pod.error_count ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {siem && (
        <div className="mt-3 pt-2 border-t border-zinc-800/40">
          <p className="text-[9px] text-zinc-600 uppercase tracking-wider mb-1.5">Infrastructure</p>
          <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[10px] mb-2">
            {redisUsed !== null && redisMax !== null ? (
              <span>
                <span className="text-zinc-600">redis </span>
                <span className={redisPct > 0.8 ? "text-rose-400" : redisPct > 0.6 ? "text-amber-400" : "text-zinc-300"}>
                  {fmtBytes(redisUsed)}/{fmtBytes(redisMax)}
                </span>
              </span>
            ) : null}
            {siem.pipeline.redis_ops_per_sec !== null ? (
              <span className="text-zinc-600">{siem.pipeline.redis_ops_per_sec.toFixed(0)} ops/s</span>
            ) : null}
            {redisUsed === null && siem.pipeline.kafka_lag.length === 0 ? (
              <span className="text-zinc-700 text-[9px]">infra metrics not exported</span>
            ) : null}
          </div>
          {siem.pipeline.kafka_lag.length > 0 && (
            <table className="w-full text-[10px] border-collapse">
              <thead>
                <tr>
                  <th className="text-left pb-0.5 pr-3 text-zinc-700 font-normal text-[9px]">topic</th>
                  <th className="text-left pb-0.5 pr-3 text-zinc-700 font-normal text-[9px]">group</th>
                  <th className="text-right pb-0.5 text-zinc-700 font-normal text-[9px]">lag</th>
                </tr>
              </thead>
              <tbody>
                {siem.pipeline.kafka_lag.slice(0, 5).map((k) => (
                  <tr key={`${k.topic}-${k.group}`}>
                    <td className="py-0.5 pr-3 text-zinc-500 truncate max-w-[130px]">{k.topic}</td>
                    <td className="py-0.5 pr-3 text-zinc-600 text-[9px]">{k.group}</td>
                    <td className={`py-0.5 text-right tabular-nums ${k.lag >= 1000 ? "text-rose-400" : k.lag >= 100 ? "text-amber-400" : "text-zinc-600"}`}>
                      {k.lag}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
