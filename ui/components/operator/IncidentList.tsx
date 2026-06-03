import { age } from "@/components/shared/fmt";
import { LANE_BORDER } from "@/components/shared/lane-tokens";
import { STATUS_LABEL, STATUS_COLOR, SEV_COLOR, type OperatorIncident } from "./types";

interface IncidentListProps {
  incidents: OperatorIncident[] | null;
  selected: string | null;
  error?: boolean;
  onSelect: (id: string | null) => void;
}

export function IncidentList({ incidents, selected, error, onSelect }: IncidentListProps) {
  const all = incidents ?? [];
  return (
    <div className="w-64 shrink-0 border-r border-zinc-800 overflow-y-auto">
      <div className="px-3 h-7 flex items-center border-b border-zinc-800/50 sticky top-0 bg-zinc-950 z-10">
        <span className="text-[9px] text-zinc-600 uppercase tracking-wider">Incidents</span>
        {incidents !== null && (
          <span className="ml-auto text-[8px] text-emerald-400 border border-emerald-400/20 px-1 rounded">live</span>
        )}
      </div>

      {incidents === null ? (
        <div className="px-3 py-3 text-[10px] text-zinc-600 animate-pulse">loading…</div>
      ) : error && all.length === 0 ? (
        <div className="px-3 py-3 text-[10px] text-rose-400/80">✕ incidents unavailable<br />(gateway /siem/overview)</div>
      ) : all.length === 0 ? (
        <div className="px-3 py-3 text-[10px] text-zinc-600">no incidents</div>
      ) : (
        all.map((inc) => {
          const isSelected = selected === inc.id;
          return (
            <button
              key={inc.id}
              onClick={() => onSelect(isSelected ? null : inc.id)}
              className={`w-full text-left px-3 py-2 border-l-2 ${LANE_BORDER[inc.lane] ?? "border-l-zinc-700"} border-b border-zinc-800/30 hover:bg-zinc-900/50 transition-colors ${isSelected ? "bg-zinc-900" : ""}`}
            >
              <div className="flex items-center justify-between gap-1 mb-0.5">
                <span className="text-[10px] text-zinc-200 truncate font-medium leading-tight">{inc.alertname}</span>
                <span className={`text-[9px] shrink-0 ${STATUS_COLOR[inc.status]}`}>{STATUS_LABEL[inc.status]}</span>
              </div>
              <div className="flex items-center justify-between gap-1">
                <span className="text-[9px] text-zinc-600 truncate">{inc.workload}</span>
                <div className="flex items-center gap-1.5 shrink-0">
                  <span className={`text-[8px] ${SEV_COLOR[inc.severity as string] ?? "text-zinc-500"}`}>{String(inc.severity).slice(0, 4).toUpperCase()}</span>
                  <span className="text-[8px] text-zinc-600">{age(inc.age_s)}</span>
                </div>
              </div>
            </button>
          );
        })
      )}
    </div>
  );
}
