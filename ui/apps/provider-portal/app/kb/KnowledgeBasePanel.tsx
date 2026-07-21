"use client";

// RAG Knowledge-Base panel. Reads live from Redis via the gateway proxy (existing +
// new entries across knowledge collections) and lets the operator add new vendor
// knowledge that gets embedded (Ollama) and fed to the diagnosis brain.
// Ported from ui/components/admin/KnowledgeBasePanel.tsx onto this portal's shared
// design system (@aoip/ui-kit + styles.css `.aoip-*` classes) — no lucide-react /
// Tailwind here, those aren't dependencies of this app.

import { useCallback, useEffect, useMemo, useState } from "react";
import { KB_TIERS, scorePillClass, tierPillClass } from "@/lib/kb";
import type { KbCreateInput, KbCreateResponse, KbItem, KbListResponse } from "@/lib/kb";
import "./kb.css";

const EMPTY_FORM: KbCreateInput = {
  title: "",
  vendor: "",
  category: "",
  tier: "basic",
  situation: "",
  knowledge: "",
  score: 70,
};

const MIN_TITLE_LENGTH = 3;
const MIN_KNOWLEDGE_LENGTH = 10;

export function KnowledgeBasePanel() {
  const [data, setData] = useState<KbListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<KbCreateInput>({ ...EMPTY_FORM });
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const res = await fetch("/api/gateway/kb", { cache: "no-store" });
      const body = (await res.json()) as KbListResponse;
      if (!res.ok) {
        setLoadError(body.error ?? `gateway trả mã ${res.status}`);
        setData(null);
      } else {
        setData(body);
      }
    } catch {
      setLoadError("Không kết nối được gateway");
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  // IIFE + cancelled-guard, no bare `load()` call — mirrors
  // app/understanding/DiagramHistoryPanel.tsx so no setState runs synchronously
  // in the effect body. `loading` already defaults to true via useState(true).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/gateway/kb", { cache: "no-store" });
        const body = (await res.json()) as KbListResponse;
        if (cancelled) return;
        if (!res.ok) {
          setLoadError(body.error ?? `gateway trả mã ${res.status}`);
          setData(null);
        } else {
          setData(body);
        }
      } catch {
        if (!cancelled) {
          setLoadError("Không kết nối được gateway");
          setData(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const items = useMemo(() => data?.items ?? [], [data]);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter(
      (item) =>
        item.title.toLowerCase().includes(q) ||
        item.vendor.toLowerCase().includes(q) ||
        item.category.toLowerCase().includes(q) ||
        item.collection.toLowerCase().includes(q),
    );
  }, [items, query]);

  async function submit() {
    if (form.title.trim().length < MIN_TITLE_LENGTH || form.knowledge.trim().length < MIN_KNOWLEDGE_LENGTH) {
      setMsg(`Tiêu đề ≥ ${MIN_TITLE_LENGTH} ký tự và nội dung ≥ ${MIN_KNOWLEDGE_LENGTH} ký tự`);
      return;
    }
    setSubmitting(true);
    setMsg(null);
    try {
      const res = await fetch("/api/gateway/kb", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(form),
      });
      const body = (await res.json()) as KbCreateResponse;
      if (!res.ok) {
        setMsg(body.detail ?? body.error ?? "Tạo thất bại");
      } else {
        setMsg(`Đã thêm ${body.id}`);
        setForm({ ...EMPTY_FORM });
        setShowForm(false);
        await load();
      }
    } catch {
      setMsg("Tạo thất bại — không kết nối được gateway");
    } finally {
      setSubmitting(false);
    }
  }

  async function remove(item: KbItem) {
    if (!confirm(`Xoá mục tri thức "${item.title.slice(0, 60)}"?`)) return;
    const res = await fetch(`/api/gateway/kb/${encodeURIComponent(item.collection)}/${encodeURIComponent(item.id)}`, {
      method: "DELETE",
    });
    if (res.ok) {
      await load();
    } else {
      setMsg("Xoá thất bại (chỉ mục do UI tạo mới xoá được)");
    }
  }

  return (
    <div className="aoip-kb" data-testid="kb-panel">
      <div className="aoip-kb-toolbar">
        <div className="aoip-muted" data-testid="kb-summary">
          {loading ? "đang tải…" : `${data?.total ?? 0} mục · nguồn: Redis (trực tiếp)`}
        </div>
        <div className="aoip-kb-toolbar-actions">
          <button type="button" className="aoip-btn" onClick={() => void load()}>
            Tải lại
          </button>
          <button
            type="button"
            className="aoip-btn"
            data-testid="kb-add-toggle"
            onClick={() => setShowForm((v) => !v)}
          >
            {showForm ? "Đóng" : "+ Thêm tri thức"}
          </button>
        </div>
      </div>

      {data?.counts && Object.keys(data.counts).length > 0 && (
        <div className="aoip-chip-row" data-testid="kb-counts">
          {Object.entries(data.counts).map(([collection, count]) => (
            <span className="aoip-chip" key={collection}>
              {collection} · {count}
            </span>
          ))}
        </div>
      )}

      {showForm && (
        <div className="aoip-kb-form" data-testid="kb-add-form">
          <div className="aoip-form-row">
            <input
              className="aoip-input"
              placeholder="Tiêu đề *"
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
            />
            <input
              className="aoip-input"
              placeholder="Nhà cung cấp (Kubernetes, Redis…)"
              value={form.vendor}
              onChange={(e) => setForm({ ...form, vendor: e.target.value })}
            />
            <input
              className="aoip-input"
              placeholder="Danh mục (memory, network…)"
              value={form.category}
              onChange={(e) => setForm({ ...form, category: e.target.value })}
            />
          </div>
          <textarea
            className="aoip-input aoip-kb-textarea"
            placeholder="Tình huống / triệu chứng áp dụng tri thức này"
            value={form.situation}
            onChange={(e) => setForm({ ...form, situation: e.target.value })}
          />
          <textarea
            className="aoip-input aoip-kb-textarea aoip-kb-textarea-lg"
            placeholder="Nội dung tri thức * — cách điều tra nguyên nhân gốc và phạm vi ảnh hưởng"
            value={form.knowledge}
            onChange={(e) => setForm({ ...form, knowledge: e.target.value })}
          />
          <div className="aoip-kb-form-footer">
            <select
              className="aoip-select"
              value={form.tier}
              onChange={(e) => setForm({ ...form, tier: e.target.value })}
            >
              {KB_TIERS.map((tier) => (
                <option key={tier} value={tier}>
                  {tier}
                </option>
              ))}
            </select>
            <label className="aoip-kb-score-input">
              điểm
              <input
                type="range"
                min={0}
                max={100}
                value={form.score}
                onChange={(e) => setForm({ ...form, score: Number(e.target.value) })}
              />
              <span>{form.score}</span>
            </label>
            <button type="button" className="aoip-btn" disabled={submitting} onClick={() => void submit()}>
              {submitting ? "Đang lưu…" : "Nhúng & lưu"}
            </button>
          </div>
        </div>
      )}

      {msg && (
        <div className="aoip-muted" data-testid="kb-msg">
          {msg}
        </div>
      )}

      <div className="aoip-filter-row">
        <input
          className="aoip-input"
          data-testid="kb-search"
          placeholder="Tìm tri thức…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {loading ? (
        <div className="aoip-state">đang tải từ Redis…</div>
      ) : loadError ? (
        <div className="aoip-err" data-testid="kb-error">
          Không tải được: {loadError}
        </div>
      ) : filtered.length === 0 ? (
        <div className="aoip-state" data-testid="kb-empty">
          không có tri thức phù hợp.
        </div>
      ) : (
        <div className="aoip-table-wrap">
          <table className="aoip-table" data-testid="kb-table">
            <thead>
              <tr>
                <th>Điểm</th>
                <th>Tiêu đề</th>
                <th>Nhà cung cấp</th>
                <th>Danh mục</th>
                <th>Cấp</th>
                <th>Bộ sưu tập</th>
                <th aria-label="Hành động" />
              </tr>
            </thead>
            <tbody>
              {filtered.map((item) => (
                <tr key={`${item.collection}:${item.id}`} data-testid={`kb-item-${item.collection}-${item.id}`}>
                  <td>
                    <span className={`aoip-pill ${scorePillClass(item.score)}`}>{item.score}</span>
                  </td>
                  <td>
                    {item.title}
                    {item.stale && (
                      <span
                        className="aoip-pill offline aoip-kb-stale"
                        title={item.stale_for && item.stale_for.length > 0 ? `stale for: ${item.stale_for.join(", ")}` : undefined}
                      >
                        cũ
                      </span>
                    )}
                  </td>
                  <td>{item.vendor || "—"}</td>
                  <td>{item.category || "—"}</td>
                  <td>
                    {item.tier ? <span className={`aoip-pill ${tierPillClass(item.tier)}`}>{item.tier}</span> : "—"}
                  </td>
                  <td className="aoip-muted">{item.collection}</td>
                  <td>
                    {item.editable && (
                      <button
                        type="button"
                        className="aoip-kb-delete"
                        onClick={() => void remove(item)}
                        aria-label={`Xoá ${item.title}`}
                      >
                        Xoá
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
