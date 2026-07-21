// RAG Knowledge-Base types — shared between the client panel (app/kb/KnowledgeBasePanel.tsx)
// and the gateway proxy (app/api/gateway/kb/*). Backing store: src/gateway/routes/kb.py
// (Redis Stack HNSW, no tenant_id — cluster-global vendor knowledge fed to the diagnosis LLM).

export interface KbItem {
  id: string;
  collection: string;
  title: string;
  vendor: string;
  category: string;
  tier: string;
  score: number;
  source: string;
  editable: boolean;
  confirmed_count?: number;
  contradicted_count?: number;
  stale?: boolean;
  stale_for?: string[];
}

export interface KbListResponse {
  items?: KbItem[];
  total?: number;
  counts?: Record<string, number>;
  write_collection?: string;
  error?: string;
}

export interface KbCreateInput {
  title: string;
  knowledge: string;
  vendor: string;
  category: string;
  tier: string;
  situation: string;
  score: number;
}

export interface KbCreateResponse {
  ok?: boolean;
  id?: string;
  collection?: string;
  detail?: string;
  error?: string;
}

/** Tiers accepted by KbCreate.tier on the gateway (src/gateway/routes/kb.py). */
export const KB_TIERS = ["basic", "intermediate", "advanced"] as const;
export type KbTier = (typeof KB_TIERS)[number];

/** Maps a KB tier to the shared `.aoip-pill` status classes (styles.css). */
export function tierPillClass(tier: string): string {
  if (tier === "advanced") return "offline";
  if (tier === "intermediate") return "stale";
  return "online";
}

/** Maps a 0-100 KB quality score to a `.aoip-pill`-compatible status class. */
export function scorePillClass(score: number): string {
  if (score >= 80) return "online";
  if (score >= 60) return "stale";
  return "unknown";
}
