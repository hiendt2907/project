"use client";

import { useEffect, useId, useState } from "react";

// Renders one Mermaid source block to inline SVG. Rendering happens fully
// client-side (dynamic import) — the gateway serves raw Mermaid text only and
// never rasterizes (see src/gateway/routes/onboarding.py get_diagram).

interface MermaidBlockProps {
  source: string;
}

export function MermaidBlock({ source }: MermaidBlockProps) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, "_");
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setSvg(null);
    setError(null);
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });
        const { svg: rendered } = await mermaid.render(`mmd_${id}`, source);
        if (!cancelled) setSvg(rendered);
      } catch (e: unknown) {
        if (!cancelled) setError(e instanceof Error ? e.message : "mermaid render failed");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id, source]);

  if (error) {
    return (
      <pre className="overflow-x-auto rounded border border-rose-900/40 bg-rose-500/5 p-2 font-mono text-[10px] text-rose-400">
        {error}
      </pre>
    );
  }
  if (!svg) {
    return <div className="h-24 animate-pulse rounded bg-zinc-800" data-testid="mermaid-loading" />;
  }
  return (
    <div
      data-testid="mermaid-svg"
      className="overflow-x-auto rounded border border-zinc-800 bg-zinc-950/60 p-2 [&_svg]:mx-auto [&_svg]:max-w-full"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}

export interface DiagramSection {
  title: string;
  source: string;
}

// The gateway diagram payload is a single text blob: 3 Mermaid diagrams
// separated by "%% <title>" comment lines (pkg.onboarding.discovery_doc
// render_all_diagrams). Split it back into titled sections.
export function splitDiagramText(text: string): DiagramSection[] {
  const sections: DiagramSection[] = [];
  let current: DiagramSection | null = null;
  for (const line of text.split("\n")) {
    if (line.startsWith("%%")) {
      if (current && current.source.trim()) sections.push(current);
      current = { title: line.replace(/^%+\s*/, "").trim(), source: "" };
      continue;
    }
    if (!current) current = { title: "diagram", source: "" };
    current = { ...current, source: current.source + line + "\n" };
  }
  if (current && current.source.trim()) sections.push(current);
  return sections;
}
