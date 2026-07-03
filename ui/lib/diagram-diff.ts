// Line-level diff for Mermaid diagram texts (small inputs: 3 diagrams / version).
// Classic LCS backtrack — good enough at this size, no dependency needed.

export type DiffOp = "same" | "added" | "removed";

export interface DiffLine {
  op: DiffOp;
  text: string;
}

export function diffLines(oldText: string, newText: string): DiffLine[] {
  const a = oldText.split("\n");
  const b = newText.split("\n");
  const m = a.length;
  const n = b.length;
  // lcs[i][j] = LCS length of a[i:], b[j:]
  const lcs: number[][] = Array.from({ length: m + 1 }, () => new Array<number>(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      out.push({ op: "same", text: a[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      out.push({ op: "removed", text: a[i] });
      i++;
    } else {
      out.push({ op: "added", text: b[j] });
      j++;
    }
  }
  for (; i < m; i++) out.push({ op: "removed", text: a[i] });
  for (; j < n; j++) out.push({ op: "added", text: b[j] });
  return out;
}

export function hasChanges(diff: DiffLine[]): boolean {
  return diff.some((l) => l.op !== "same");
}
