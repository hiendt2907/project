"use client";

// T2 advisory + T3 session drill-down for a selected incident.

import { useState } from "react";
import { age } from "@/components/shared/fmt";
import { LANE_BORDER, LANE_TEXT, LANE_DOT, LANE_LABEL } from "@/components/shared/lane-tokens";
import { TraceSessionView } from "@/components/shared/TraceSessionView";
import { STATUS_LABEL, STATUS_COLOR, SEV_COLOR, type OperatorIncident } from "./types";

export function AdvisoryPanel({ incident }: { incident: OperatorIncident }) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const [showSession, setShowSession] = useState(false);

  return (
    <div className="p-4 font-mono text-[10px]">
      {/* Header */}
      <div className={`border-l-2 ${LANE_BORDER[incident.lane] ?? "border-l-zinc-700"} pl-3 mb-4`}>
        <div className="flex items-start justify-between gap-2 mb-0.5">
          <h2 className="text-[11px] font-semibold text-zinc-200 leading-tight">{incident.alertname}</h2>
          <span className={`shrink-0 ${STATUS_COLOR[incident.status]}`}>{STATUS_LABEL[incident.status]}</span>
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[9px] text-zinc-600">
          <span>{incident.namespace}/{incident.workload}</span>
          <span>·</span>
          <span className="font-mono">{incident.trace_id?.slice(0, 14)}</span>
          <span>·</span>
          <span className={SEV_COLOR[incident.severity as string] ?? "text-zinc-500"}>{String(incident.severity).toUpperCase()}</span>
          <span>·</span>
          <span>{age(incident.age_s)} ago</span>
          <span>·</span>
          <span className={LANE_TEXT[incident.lane] ?? "text-zinc-500"}>{LANE_LABEL[incident.lane]}</span>
        </div>
      </div>

      {/* Root cause */}
      <div className="mb-4">
        <p className="text-[9px] text-zinc-600 uppercase tracking-wider mb-1">Root Cause</p>
        <p className="text-zinc-300 leading-relaxed">{incident.root_cause}</p>
      </div>

      {/* Events / Verification */}
      <div className="mb-4">
        <p className="text-[9px] text-zinc-600 uppercase tracking-wider mb-1">
          {incident.verification_steps[0]?.layer === "INFO" ? "Event Timeline" : "Verification"}
        </p>
        <div className="space-y-0.5">
          {incident.verification_steps.map((step, i) => (
            <button
              key={i}
              onClick={() => setExpanded(expanded === i ? null : i)}
              className="w-full text-left py-1 px-2 hover:bg-zinc-900/50 rounded transition-colors"
            >
              <div className="flex items-start gap-2">
                <span className="text-[8px] text-zinc-600 bg-zinc-800/60 px-1 py-0.5 rounded shrink-0">{step.layer}</span>
                <code className="text-[9px] text-emerald-300 font-mono flex-1 text-left leading-relaxed break-all">{step.command}</code>
              </div>
              {expanded === i && <p className="text-[9px] text-zinc-600 mt-1 pl-8">{step.rationale}</p>}
            </button>
          ))}
        </div>
      </div>

      {/* Suggested action */}
      <div className="mb-4">
        <p className="text-[9px] text-zinc-600 uppercase tracking-wider mb-1">Suggested Action</p>
        <div className="flex items-start gap-2">
          <span className="text-emerald-400 shrink-0">→</span>
          <p className="text-zinc-300">{incident.suggested_action}</p>
        </div>
      </div>

      {/* HITL */}
      {incident.hitl_id && (
        <div className="p-2 bg-rose-500/5 border border-rose-500/20 rounded flex items-center gap-2 mb-4">
          <span className="text-rose-400">⚠</span>
          <span className="text-rose-300">HITL approval required</span>
          <span className="text-zinc-600 font-mono ml-2 text-[9px]">{incident.hitl_id}</span>
        </div>
      )}

      {/* T3 · Diagnosis session drill-down */}
      <div className="border-t border-zinc-800/50 pt-3">
        <button
          onClick={() => setShowSession((s) => !s)}
          className="flex items-center gap-2 text-[9px] text-amber-400 hover:text-amber-300 uppercase tracking-wider mb-2"
        >
          <span className={`transition-transform ${showSession ? "rotate-90" : ""}`}>▸</span>
          Diagnosis Session (multi-turn)
        </button>
        {showSession && incident.trace_id && (
          <div className="pl-1">
            <TraceSessionView traceId={incident.trace_id} />
          </div>
        )}
      </div>

      {/* Lane dot */}
      <div className="mt-4 pt-3 border-t border-zinc-800/50 flex items-center gap-1.5">
        <span className={`w-1.5 h-1.5 rounded-full ${LANE_DOT[incident.lane] ?? "bg-zinc-600"}`} />
        <span className="text-[9px] text-zinc-600">{LANE_LABEL[incident.lane]}</span>
      </div>
    </div>
  );
}
