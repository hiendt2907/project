"use client";

// RAG Knowledge-Base panel. Reads live from Redis via gateway /kb (existing + new
// entries across knowledge collections), shows a per-KB quality score, and lets the
// operator add new vendor knowledge that is embedded and fed to the diagnosis brain.

import { useCallback, useEffect, useMemo, useState } from "react";
import { Brain, Plus, Trash2, Search, RefreshCw, Loader2 } from "lucide-react";

interface KbItem {
  id: string;
  collection: string;
  title: string;
  vendor: string;
  category: string;
  tier: string;
  score: number;
  source: string;
  editable: boolean;
}

interface KbResponse {
  items?: KbItem[];
  total?: number;
  counts?: Record<string, number>;
  write_collection?: string;
  source?: string;
  error?: string;
}

const TIER_COLOR: Record<string, string> = {
  basic: "text-emerald-400 border-emerald-900/60",
  intermediate: "text-amber-400 border-amber-900/60",
  advanced: "text-rose-400 border-rose-900/60",
};

function scoreColor(s: number): string {
  if (s >= 80) return "text-emerald-400";
  if (s >= 60) return "text-amber-400";
  return "text-zinc-500";
}

const EMPTY_FORM = {
  title: "",
  vendor: "",
  category: "",
  tier: "basic",
  situation: "",
  knowledge: "",
  score: 70,
};

export function KnowledgeBasePanel() {
  const [data, setData] = useState<KbResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/kb?limit=300", { cache: "no-store" });
      setData(await res.json());
    } catch {
      setData({ source: "error", error: "fetch failed" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const items = data?.items ?? [];
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter(
      (i) =>
        i.title.toLowerCase().includes(q) ||
        i.vendor.toLowerCase().includes(q) ||
        i.category.toLowerCase().includes(q) ||
        i.collection.toLowerCase().includes(q)
    );
  }, [items, query]);

  async function submit() {
    if (form.title.trim().length < 3 || form.knowledge.trim().length < 10) {
      setMsg("Title ≥ 3 chars and knowledge ≥ 10 chars required");
      return;
    }
    setSubmitting(true);
    setMsg(null);
    try {
      const res = await fetch("/api/kb", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      const body = await res.json();
      if (!res.ok) {
        setMsg(body?.detail ?? body?.error ?? "create failed");
      } else {
        setMsg(`Added ${body.id}`);
        setForm({ ...EMPTY_FORM });
        setShowForm(false);
        await load();
      }
    } catch {
      setMsg("create failed");
    } finally {
      setSubmitting(false);
    }
  }

  async function remove(item: KbItem) {
    if (!confirm(`Delete KB "${item.title.slice(0, 60)}"?`)) return;
    const res = await fetch(`/api/kb/${encodeURIComponent(item.collection)}/${encodeURIComponent(item.id)}`, {
      method: "DELETE",
    });
    if (res.ok) await load();
    else setMsg("delete failed (only UI-created entries are deletable)");
  }

  return (
    <div className="flex flex-col gap-4">
      <header className="flex items-center gap-3">
        <Brain className="text-cyan-400" size={20} />
        <div className="flex-1">
          <h1 className="text-sm font-bold uppercase tracking-widest text-zinc-200">RAG Knowledge Base</h1>
          <p className="text-[10px] text-zinc-600">
            Live from Redis · {data?.total ?? 0} entries · fed to the diagnosis second brain
          </p>
        </div>
        <button
          onClick={() => void load()}
          className="flex items-center gap-1.5 text-[10px] text-zinc-400 hover:text-zinc-200 border border-zinc-800 rounded px-2 py-1"
        >
          <RefreshCw size={12} /> Refresh
        </button>
        <button
          onClick={() => setShowForm((v) => !v)}
          className="flex items-center gap-1.5 text-[10px] font-bold text-cyan-300 border border-cyan-900/60 bg-cyan-950/20 rounded px-2.5 py-1 hover:bg-cyan-950/40"
        >
          <Plus size={12} /> Add knowledge
        </button>
      </header>

      {data?.counts && (
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(data.counts).map(([col, n]) => (
            <span key={col} className="text-[9px] text-zinc-500 border border-zinc-800 rounded px-1.5 py-0.5">
              <span className="text-zinc-400">{col}</span> {n}
            </span>
          ))}
        </div>
      )}

      {showForm && (
        <div className="border border-cyan-900/40 bg-cyan-950/10 rounded-lg p-3 flex flex-col gap-2">
          <div className="grid grid-cols-4 gap-2">
            <input
              className="col-span-2 bg-zinc-950 border border-zinc-800 rounded px-2 py-1.5 text-[11px] text-zinc-200"
              placeholder="Title *"
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
            />
            <input
              className="bg-zinc-950 border border-zinc-800 rounded px-2 py-1.5 text-[11px] text-zinc-200"
              placeholder="Vendor (Kubernetes, Redis…)"
              value={form.vendor}
              onChange={(e) => setForm({ ...form, vendor: e.target.value })}
            />
            <input
              className="bg-zinc-950 border border-zinc-800 rounded px-2 py-1.5 text-[11px] text-zinc-200"
              placeholder="Category (memory, network…)"
              value={form.category}
              onChange={(e) => setForm({ ...form, category: e.target.value })}
            />
          </div>
          <textarea
            className="bg-zinc-950 border border-zinc-800 rounded px-2 py-1.5 text-[11px] text-zinc-200 min-h-[44px]"
            placeholder="Situation / symptom this knowledge applies to"
            value={form.situation}
            onChange={(e) => setForm({ ...form, situation: e.target.value })}
          />
          <textarea
            className="bg-zinc-950 border border-zinc-800 rounded px-2 py-1.5 text-[11px] text-zinc-200 min-h-[80px]"
            placeholder="Knowledge * — how to investigate the root cause and trace blast radius"
            value={form.knowledge}
            onChange={(e) => setForm({ ...form, knowledge: e.target.value })}
          />
          <div className="flex items-center gap-3">
            <select
              className="bg-zinc-950 border border-zinc-800 rounded px-2 py-1.5 text-[11px] text-zinc-200"
              value={form.tier}
              onChange={(e) => setForm({ ...form, tier: e.target.value })}
            >
              <option value="basic">basic</option>
              <option value="intermediate">intermediate</option>
              <option value="advanced">advanced</option>
            </select>
            <label className="flex items-center gap-2 text-[10px] text-zinc-500">
              score
              <input
                type="range"
                min={0}
                max={100}
                value={form.score}
                onChange={(e) => setForm({ ...form, score: Number(e.target.value) })}
              />
              <span className={scoreColor(form.score)}>{form.score}</span>
            </label>
            <button
              onClick={() => void submit()}
              disabled={submitting}
              className="ml-auto flex items-center gap-1.5 text-[11px] font-bold text-zinc-950 bg-cyan-400 rounded px-3 py-1.5 disabled:opacity-50"
            >
              {submitting ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />} Embed & save
            </button>
          </div>
        </div>
      )}

      {msg && <div className="text-[10px] text-amber-400">{msg}</div>}

      <div className="relative">
        <Search size={12} className="absolute left-2 top-2 text-zinc-600" />
        <input
          className="w-full bg-zinc-950 border border-zinc-800 rounded pl-7 pr-2 py-1.5 text-[11px] text-zinc-200"
          placeholder="Search knowledge…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {loading ? (
        <div className="text-[11px] text-zinc-600 animate-pulse">loading from Redis…</div>
      ) : data?.source === "error" ? (
        <div className="text-[11px] text-rose-400">Gateway unavailable: {data.error}</div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {filtered.map((i) => (
            <div
              key={`${i.collection}:${i.id}`}
              className="group flex items-start gap-3 border border-zinc-800/70 rounded-lg bg-zinc-950/40 px-3 py-2 hover:border-zinc-700"
            >
              <div className={`shrink-0 text-center font-mono ${scoreColor(i.score)}`}>
                <div className="text-base font-bold leading-none">{i.score}</div>
                <div className="text-[7px] text-zinc-700 uppercase">score</div>
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-[11px] text-zinc-200 leading-snug">{i.title}</p>
                <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                  {i.vendor && <span className="text-[8px] text-cyan-400/80 border border-cyan-900/40 rounded px-1">{i.vendor}</span>}
                  {i.category && <span className="text-[8px] text-zinc-500 border border-zinc-800 rounded px-1">{i.category}</span>}
                  {i.tier && (
                    <span className={`text-[8px] border rounded px-1 ${TIER_COLOR[i.tier] ?? "text-zinc-500 border-zinc-800"}`}>{i.tier}</span>
                  )}
                  <span className="text-[8px] text-zinc-700">{i.collection}</span>
                  {i.source && <span className="text-[8px] text-zinc-700">· {i.source}</span>}
                </div>
              </div>
              {i.editable && (
                <button
                  onClick={() => void remove(i)}
                  className="opacity-0 group-hover:opacity-100 text-zinc-600 hover:text-rose-400 transition"
                  title="Delete"
                >
                  <Trash2 size={13} />
                </button>
              )}
            </div>
          ))}
          {filtered.length === 0 && <div className="text-[11px] text-zinc-600">no knowledge matches.</div>}
        </div>
      )}
    </div>
  );
}
