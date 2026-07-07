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
