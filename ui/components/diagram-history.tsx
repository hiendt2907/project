"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { diffLines, hasChanges, type DiffLine } from "@/lib/diagram-diff";

// History/diff panel for the System Diagram card. Fetches versions newest-first
// from /api/onboarding/diagram-history (anchored at latest) and shows a
// line-level diff of the selected version against the next-older one.

interface DiagramVersion {
  version: number;
  mermaid: string;
}

interface HistoryResponse {
  latest: number | null;
  versions: DiagramVersion[];
  next_before: number | null;
  error?: string;
}

const OP_STYLES: Record<DiffLine["op"], string> = {
  added: "bg-emerald-500/10 text-emerald-400",
  removed: "bg-rose-500/10 text-rose-400",
  same: "text-zinc-500",
};

const OP_PREFIX: Record<DiffLine["op"], string> = { added: "+", removed: "-", same: " " };

export function DiagramHistoryPanel({ tenant }: { tenant: string }) {
  const [versions, setVersions] = useState<DiagramVersion[]>([]);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<number | null>(null);

  const load = useCallback(
    async (before: number | null) => {
      setLoading(true);
      setError(null);
      try {
        const search = new URLSearchParams({ tenant_id: tenant, limit: "10" });
        if (before !== null) search.set("before", String(before));
        const res = await fetch(`/api/onboarding/diagram-history?${search.toString()}`, { cache: "no-store" });
        const body = (await res.json().catch(() => null)) as HistoryResponse | null;
        if (!res.ok || !body) {
          setError(body?.error ?? `HTTP ${res.status}`);
          return;
        }
        setVersions((prev) => (before === null ? body.versions : [...prev, ...body.versions]));
        setNextBefore(body.next_before);
      } catch {
        setError("request failed");
      } finally {
        setLoading(false);
      }
    },
    [tenant],
  );

  useEffect(() => {
    setVersions([]);
    setSelected(null);
    load(null);
  }, [load]);

  const selectedIdx = versions.findIndex((v) => v.version === selected);
  const current = selectedIdx >= 0 ? versions[selectedIdx] : null;
  const previous = selectedIdx >= 0 ? (versions[selectedIdx + 1] ?? null) : null;
  const diff = current && previous ? diffLines(previous.mermaid, current.mermaid) : null;

  return (
    <div data-testid="diagram-history-panel" className="mt-4 rounded border border-zinc-800 bg-zinc-950/40 p-3">
      <p className="mb-2 text-[10px] font-semibold uppercase tracking-widest text-zinc-600">
        Version history (newest first)
      </p>
      {error ? (
        <div className="flex items-center gap-2 rounded border border-rose-900/40 bg-rose-500/5 px-3 py-2 text-xs text-rose-400">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          {error}
        </div>
      ) : loading && versions.length === 0 ? (
        <div className="h-8 animate-pulse rounded bg-zinc-800" />
      ) : versions.length === 0 ? (
        <p className="text-xs text-zinc-500">No diagram versions recorded for this tenant.</p>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-1.5">
            {versions.map((v) => (
              <button
                key={v.version}
                data-testid="diagram-history-version"
                onClick={() => setSelected(v.version === selected ? null : v.version)}
                className={`rounded border px-2 py-0.5 font-mono text-[11px] transition-colors ${
                  selected === v.version
                    ? "border-cyan-500/40 bg-cyan-500/10 text-cyan-300"
                    : "border-zinc-800 bg-zinc-900/40 text-zinc-400 hover:border-zinc-600"
                }`}
              >
                v{v.version}
              </button>
            ))}
            {nextBefore !== null && (
              <button
                onClick={() => load(nextBefore)}
                disabled={loading}
                className="rounded border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-500 transition-colors hover:border-zinc-600 hover:text-zinc-300 disabled:opacity-40"
              >
                {loading ? "Loading…" : "Older…"}
              </button>
            )}
          </div>
          {current && (
            <div className="mt-3">
              {!previous ? (
                <p className="text-xs text-zinc-500">
                  No older version loaded to diff against — load older versions or pick another.
                </p>
              ) : diff && hasChanges(diff) ? (
                <>
                  <p className="mb-1 font-mono text-[10px] text-zinc-600">
                    diff v{previous.version} → v{current.version}
                  </p>
                  <pre className="max-h-72 overflow-auto rounded border border-zinc-800 bg-zinc-950/60 p-2 font-mono text-[10px] leading-relaxed">
                    {diff.map((line, i) => (
                      <div key={i} data-testid={`diff-${line.op}`} className={OP_STYLES[line.op]}>
                        {OP_PREFIX[line.op]} {line.text}
                      </div>
                    ))}
                  </pre>
                </>
              ) : (
                <p data-testid="diff-identical" className="text-xs text-zinc-500">
                  v{current.version} is identical to v{previous.version} (regenerated without structural change).
                </p>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
