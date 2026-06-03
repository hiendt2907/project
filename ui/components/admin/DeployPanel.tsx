import { SectionLabel, Loading, Unavailable } from "@/components/shared/primitives";
import type { DeployEntry } from "./types";

export function DeployPanel({ deploy, error }: { deploy: DeployEntry[] | null; error?: boolean }) {
  if (error && deploy === null) {
    return (
      <div>
        <SectionLabel text="G · Deploy State" />
        <Unavailable detail="deploy state unavailable (gateway /agents)" />
      </div>
    );
  }
  return (
    <div>
      <SectionLabel text="G · Deploy State" note={deploy === null ? <Loading /> : undefined} />
      {deploy !== null && (
        <table className="w-full text-[10px] border-collapse">
          <thead>
            <tr>
              <th className="text-left pb-1 pr-4 text-zinc-600 font-normal">role</th>
              <th className="text-left pb-1 pr-4 text-zinc-600 font-normal">status</th>
              <th className="text-left pb-1 pr-4 text-zinc-600 font-normal">version</th>
              <th className="text-left pb-1 pr-4 text-zinc-600 font-normal">replicas</th>
              <th className="text-left pb-1 text-zinc-600 font-normal">deployed</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/30">
            {deploy.map((c) => (
              <tr key={c.name} className="hover:bg-zinc-900/40">
                <td className="py-1 pr-4 text-zinc-200">{c.role}</td>
                <td className={`py-1 pr-4 ${c.status === "running" ? "text-emerald-400" : c.status === "degraded" ? "text-amber-400" : "text-rose-400"}`}>
                  {c.status === "running" ? "● run" : c.status === "degraded" ? "▲ degraded" : "✕ down"}
                </td>
                <td className="py-1 pr-4 text-zinc-400 max-w-[100px] truncate">{c.version}</td>
                <td className="py-1 pr-4 text-zinc-400">{c.replicas}</td>
                <td className="py-1 text-zinc-600">{new Date(c.last_deployed).toLocaleDateString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
