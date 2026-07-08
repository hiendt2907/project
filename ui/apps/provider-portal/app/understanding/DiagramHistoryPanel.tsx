"use client";

import { useCallback, useEffect, useState } from "react";
import { diffLines, hasChanges, type DiffLine } from "@/lib/diagram-diff";

// History/diff panel for the System Diagram card. Fetches versions newest-first
// from /api/onboarding/diagram-history (anchored at latest) and shows a
// line-level diff of the selected version against the next-older one.
// Ported from ui/components/diagram-history.tsx (Productization Iteration 23),
// restyled with @aoip/ui-kit aoip-* classes instead of Tailwind.

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

  // `tenant` is fixed for the lifetime of this component instance (the parent
  // page renders one DiagramHistoryPanel per tenant, not a shared/reused one).
  // IIFE + cancelled-guard (not a bare `load(null)` call) so no setState runs
  // synchronously in the effect body — mirrors components/mermaid-diagram.tsx.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const search = new URLSearchParams({ tenant_id: tenant, limit: "10" });
      try {
        const res = await fetch(`/api/onboarding/diagram-history?${search.toString()}`, { cache: "no-store" });
        const body = (await res.json().catch(() => null)) as HistoryResponse | null;
        if (cancelled) return;
        if (!res.ok || !body) {
          setError(body?.error ?? `HTTP ${res.status}`);
        } else {
          setVersions(body.versions);
          setNextBefore(body.next_before);
        }
      } catch {
        if (!cancelled) setError("request failed");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tenant]);

  const selectedIdx = versions.findIndex((v) => v.version === selected);
  const current = selectedIdx >= 0 ? versions[selectedIdx] : null;
  const previous = selectedIdx >= 0 ? (versions[selectedIdx + 1] ?? null) : null;
  const diff = current && previous ? diffLines(previous.mermaid, current.mermaid) : null;

  return (
    <div data-testid="diagram-history-panel" className="aoip-history">
      <div className="aoip-muted">Version history (newest first)</div>
      {error ? (
        <div className="aoip-err">{error}</div>
      ) : loading && versions.length === 0 ? (
        <div className="aoip-muted">Loading…</div>
      ) : versions.length === 0 ? (
        <div className="aoip-muted">No diagram versions recorded for this tenant.</div>
      ) : (
        <>
          <div className="aoip-history-versions">
            {versions.map((v) => (
              <button
                key={v.version}
                type="button"
                data-testid="diagram-history-version"
                onClick={() => setSelected(v.version === selected ? null : v.version)}
                className={`aoip-history-version${selected === v.version ? " selected" : ""}`}
              >
                v{v.version}
              </button>
            ))}
            {nextBefore !== null && (
              <button
                type="button"
                className="aoip-history-version"
                onClick={() => load(nextBefore)}
                disabled={loading}
              >
                {loading ? "Loading…" : "Older…"}
              </button>
            )}
          </div>
          {current && (
            <div>
              {!previous ? (
                <div className="aoip-muted">
                  No older version loaded to diff against — load older versions or pick another.
                </div>
              ) : diff && hasChanges(diff) ? (
                <>
                  <div className="aoip-muted">diff v{previous.version} → v{current.version}</div>
                  <pre className="aoip-diff">
                    {diff.map((line, i) => (
                      <div key={i} data-testid={`diff-${line.op}`} className={`aoip-diff-line ${line.op}`}>
                        {OP_PREFIX[line.op]} {line.text}
                      </div>
                    ))}
                  </pre>
                </>
              ) : (
                <div data-testid="diff-identical" className="aoip-muted">
                  v{current.version} is identical to v{previous.version} (regenerated without structural change).
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
