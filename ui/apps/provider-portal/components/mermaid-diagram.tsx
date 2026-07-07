"use client";

import { useEffect, useId, useState } from "react";

// Renders one Mermaid source block to inline SVG. Rendering happens fully
// client-side (dynamic import) — the gateway serves raw Mermaid text only and
// never rasterizes (see src/gateway/routes/onboarding.py get_diagram). Ported
// from ui/components/mermaid-diagram.tsx (legacy omni-ui), restyled with
// @aoip/ui-kit aoip-* classes instead of Tailwind.

interface MermaidBlockProps {
  source: string;
}

interface RenderResult {
  source: string;
  svg: string | null;
  error: string | null;
}

export function MermaidBlock({ source }: MermaidBlockProps) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, "_");
  // Keyed by `source` so a change is visible as "stale" without a synchronous
  // setState at the top of the effect (react-hooks/set-state-in-effect).
  const [result, setResult] = useState<RenderResult>({ source: "", svg: null, error: null });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });
        const { svg: rendered } = await mermaid.render(`mmd_${id}`, source);
        if (!cancelled) setResult({ source, svg: rendered, error: null });
      } catch (e: unknown) {
        if (!cancelled) {
          setResult({ source, svg: null, error: e instanceof Error ? e.message : "mermaid render failed" });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id, source]);

  const stale = result.source !== source;
  if (!stale && result.error) {
    return (
      <pre className="aoip-err" data-testid="mermaid-error">
        {result.error}
      </pre>
    );
  }
  if (stale || !result.svg) {
    return <div className="aoip-muted" data-testid="mermaid-loading">Đang dựng sơ đồ…</div>;
  }
  return (
    <div
      data-testid="mermaid-svg"
      className="aoip-diagram"
      dangerouslySetInnerHTML={{ __html: result.svg }}
    />
  );
}

// splitDiagramText/DiagramSection moved to lib/diagram-utils.ts — this file
// has "use client", which would turn a pure text-splitting function into a
// client reference and break server-component callers (app/understanding/page.tsx).
