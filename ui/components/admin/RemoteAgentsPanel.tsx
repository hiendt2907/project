import { useCallback, useState } from "react";
import { SectionLabel, Loading, Unavailable } from "@/components/shared/primitives";
import { age } from "@/components/shared/fmt";
import type { RemoteAgentsResponse, RemoteAgent } from "@/app/api/remote-agents/route";
import type { AgentLogEntry } from "./types";
import { PROBE_COLOR } from "./types";

function AgentProbeLog({ logs, now }: { logs: AgentLogEntry[] | undefined; now: number }) {
  if (logs === undefined) return <div className="py-1.5 text-[9px] text-zinc-600 animate-pulse">loading probe history…</div>;
  if (logs.length === 0) return <div className="py-1.5 text-[9px] text-zinc-600">no probe data</div>;
  return (
    <div className="pt-1 space-y-0.5">
      {logs.slice(0, 6).map((entry, i) => {
        const ageSec = Math.max(0, Math.floor(now / 1000 - Number(entry.ts)));
        return (
          <div key={`${entry.ts}-${i}`} className="flex items-center gap-2 text-[9px]">
            <span className={`w-14 shrink-0 ${PROBE_COLOR[entry.result] ?? "text-zinc-600"}`}>{entry.result}</span>
            <span className="text-zinc-500 truncate flex-1">{entry.probe}</span>
            <span className="text-zinc-600 shrink-0">{age(ageSec)}</span>
          </div>
        );
      })}
    </div>
  );
}

interface RemoteAgentsPanelProps {
  remoteAgents: RemoteAgentsResponse | null;
  tenant: string;
  now: number;
  error?: boolean;
}

export function RemoteAgentsPanel({ remoteAgents, tenant, now, error }: RemoteAgentsPanelProps) {
  const [expandedAgent, setExpandedAgent] = useState<string | null>(null);
  const [agentLogs, setAgentLogs] = useState<Record<string, AgentLogEntry[]>>({});

  const toggleAgent = useCallback((agentId: string) => {
    setExpandedAgent((prev) => (prev === agentId ? null : agentId));
    setAgentLogs((prev) => {
      if (prev[agentId] !== undefined) return prev;
      fetch(`/api/remote-agents/logs?agent_id=${encodeURIComponent(agentId)}&n=10`)
        .then((r) => r.json())
        .then((data: { logs?: AgentLogEntry[] }) => setAgentLogs((p) => ({ ...p, [agentId]: data.logs ?? [] })))
        .catch(() => setAgentLogs((p) => ({ ...p, [agentId]: [] })));
      return { ...prev, [agentId]: undefined as unknown as AgentLogEntry[] };
    });
  }, []);

  const filteredAgents =
    remoteAgents?.agents.filter(
      (a) =>
        !("tenant_id" in a) ||
        (a as RemoteAgent & { tenant_id?: string }).tenant_id === tenant ||
        tenant === "default"
    ) ?? [];

  return (
    <div>
      <SectionLabel
        text={`F · Remote Agents · ${tenant}`}
        note={error && remoteAgents === null ? undefined : remoteAgents === null ? <Loading /> : undefined}
      />
      {error && remoteAgents === null ? (
        <Unavailable detail="agent registry unavailable (gateway /agents/remote)" />
      ) : remoteAgents !== null && filteredAgents.length === 0 ? (
        <div className="text-[10px] text-zinc-600">no agents registered</div>
      ) : (
        <div className="divide-y divide-zinc-800/30">
          {filteredAgents.map((agent) => {
            const ageSec = Math.max(0, Math.floor(now / 1000 - agent.last_seen));
            const m = agent.metrics;
            const isExpanded = expandedAgent === agent.agent_id;
            const eps = agent.eps !== undefined ? `${agent.eps.toFixed(2)} eps` : agent.evidence_count !== undefined ? `${agent.evidence_count} ev` : "";
            return (
              <div key={agent.agent_id}>
                <button className="w-full text-left py-1.5 hover:bg-zinc-900/40 transition-colors" onClick={() => toggleAgent(agent.agent_id)}>
                  <div className="flex items-center gap-2 text-[10px]">
                    <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${agent.online ? "bg-emerald-400" : "bg-zinc-600"}`} />
                    <span className="text-zinc-200 truncate flex-1">{agent.hostname}</span>
                    <span className="text-zinc-600 text-[9px]">{agent.capabilities.map((c) => c.slice(0, 3).toUpperCase()).join("/")}</span>
                    {m && (
                      <>
                        <span className={`text-[9px] ${m.cpu_percent >= 90 ? "text-rose-400" : m.cpu_percent >= 80 ? "text-amber-400" : "text-zinc-600"}`}>cpu:{m.cpu_percent.toFixed(0)}%</span>
                        <span className={`text-[9px] ${m.mem_percent >= 90 ? "text-rose-400" : m.mem_percent >= 85 ? "text-amber-400" : "text-zinc-600"}`}>mem:{m.mem_percent.toFixed(0)}%</span>
                      </>
                    )}
                    {eps && <span className="text-zinc-600 text-[9px]">{eps}</span>}
                    <span className="text-zinc-600 text-[9px]">{age(ageSec)}</span>
                    <span className={`text-zinc-600 text-[9px] transition-transform ${isExpanded ? "rotate-180" : ""}`}>▾</span>
                  </div>
                </button>
                {isExpanded && (
                  <div className="pl-3.5 pb-1.5 border-t border-zinc-800/40">
                    <AgentProbeLog logs={agentLogs[agent.agent_id]} now={now} />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
