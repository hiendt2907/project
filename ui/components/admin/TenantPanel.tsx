"use client";

import { useCallback, useEffect, useState } from "react";
import { SectionLabel, Unavailable } from "@/components/shared/primitives";

// Tenant / API-key management persisted to PostgreSQL (MASTER_PLAN §6.7).
// Keys: plaintext shown ONCE on creation; only prefix + created_at stored after.

interface TenantRow {
  tenant_id: string;
  display_name: string;
  status: "active" | "suspended";
  created_at: string | null;
  active_keys: number;
}

interface ApiKeyRow {
  id: number;
  key_prefix: string;
  label: string | null;
  status: "active" | "revoked";
  created_by: string;
  created_at: string | null;
  revoked_at: string | null;
}

export function TenantPanel() {
  const [tenants, setTenants] = useState<TenantRow[] | null>(null);
  const [loadErr, setLoadErr] = useState(false);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [newId, setNewId] = useState("");
  const [newName, setNewName] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [keys, setKeys] = useState<Record<string, ApiKeyRow[]>>({});
  const [revealed, setRevealed] = useState<string>("");

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/autonomy/tenants", { cache: "no-store" });
      if (!res.ok) return setLoadErr(true);
      const data = (await res.json()) as { tenants: TenantRow[] };
      setTenants(data.tenants);
      setLoadErr(false);
    } catch {
      setLoadErr(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const loadKeys = useCallback(async (tid: string) => {
    try {
      const res = await fetch(`/api/autonomy/tenants/${encodeURIComponent(tid)}/api-keys`, { cache: "no-store" });
      if (!res.ok) return;
      const data = (await res.json()) as { api_keys: ApiKeyRow[] };
      setKeys((prev) => ({ ...prev, [tid]: data.api_keys }));
    } catch {
      /* ignore */
    }
  }, []);

  const toggleExpand = useCallback(
    (tid: string) => {
      setExpanded((cur) => {
        const next = cur === tid ? null : tid;
        if (next) void loadKeys(next);
        return next;
      });
    },
    [loadKeys],
  );

  const createTenant = useCallback(async () => {
    setBusy(true);
    setMsg("");
    try {
      const res = await fetch("/api/autonomy/tenants", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tenant_id: newId, display_name: newName || newId, actor: "admin_ui" }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) setMsg(`✗ ${data.detail ?? data.error ?? res.status}`);
      else {
        setMsg(`✓ tenant ${newId} created`);
        setNewId("");
        setNewName("");
        await load();
      }
    } catch {
      setMsg("✗ network error");
    } finally {
      setBusy(false);
    }
  }, [newId, newName, load]);

  const setStatus = useCallback(
    async (tid: string, status: "active" | "suspended") => {
      setBusy(true);
      try {
        await fetch(`/api/autonomy/tenants/${encodeURIComponent(tid)}/status`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status, actor: "admin_ui" }),
        });
        await load();
      } finally {
        setBusy(false);
      }
    },
    [load],
  );

  const createKey = useCallback(
    async (tid: string) => {
      setBusy(true);
      setRevealed("");
      try {
        const res = await fetch(`/api/autonomy/tenants/${encodeURIComponent(tid)}/api-keys`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ actor: "admin_ui" }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.api_key) {
          setRevealed(data.api_key);
          setMsg(`✓ key created — copy now (shown once)`);
          await loadKeys(tid);
          await load();
        } else {
          setMsg(`✗ ${data.detail ?? data.error ?? res.status}`);
        }
      } finally {
        setBusy(false);
      }
    },
    [loadKeys, load],
  );

  const revokeKey = useCallback(
    async (tid: string, keyId: number) => {
      if (!window.confirm(`Revoke key #${keyId}?`)) return;
      setBusy(true);
      try {
        await fetch(`/api/autonomy/tenants/${encodeURIComponent(tid)}/api-keys/${keyId}`, { method: "DELETE" });
        await loadKeys(tid);
        await load();
      } finally {
        setBusy(false);
      }
    },
    [loadKeys, load],
  );

  if (loadErr) {
    return (
      <div>
        <SectionLabel text="Tenant / API Keys" />
        <Unavailable detail="gateway /autonomy/tenants unreachable" />
      </div>
    );
  }

  return (
    <div>
      <SectionLabel text="Tenant / API Keys" note={<span className="text-zinc-600">PostgreSQL · key shown once</span>} />
      <div className="border border-zinc-800 bg-zinc-900/40">
        <div className="max-h-72 overflow-auto">
          {tenants === null ? (
            <div className="p-3 text-zinc-600 text-[10px]">loading…</div>
          ) : (
            tenants.map((t) => (
              <div key={t.tenant_id} className="border-b border-zinc-800/60">
                <div className="flex items-center justify-between px-2 py-1.5 text-[10px]">
                  <button onClick={() => toggleExpand(t.tenant_id)} className="flex items-center gap-2 text-left">
                    <span className="text-zinc-500">{expanded === t.tenant_id ? "▾" : "▸"}</span>
                    <span className="text-zinc-200">{t.tenant_id}</span>
                    <span className="text-zinc-600">{t.display_name}</span>
                    <span className="text-zinc-700">{t.active_keys} keys</span>
                  </button>
                  <div className="flex items-center gap-1">
                    <span className={t.status === "active" ? "text-emerald-400" : "text-rose-400"}>{t.status}</span>
                    <button
                      disabled={busy}
                      onClick={() => setStatus(t.tenant_id, t.status === "active" ? "suspended" : "active")}
                      className="px-1.5 py-0.5 border border-zinc-700 text-zinc-400 text-[9px] hover:border-zinc-500"
                    >
                      {t.status === "active" ? "suspend" : "activate"}
                    </button>
                  </div>
                </div>
                {expanded === t.tenant_id && (
                  <div className="px-3 pb-2 bg-zinc-950/40">
                    {(keys[t.tenant_id] ?? []).map((k) => (
                      <div key={k.id} className="flex items-center justify-between py-0.5 text-[10px]">
                        <span className="text-zinc-400">
                          <code className="text-amber-500/80">{k.key_prefix}…</code>
                          <span className="ml-2 text-zinc-700">{k.created_at?.slice(0, 10)}</span>
                        </span>
                        <span className="flex items-center gap-1">
                          <span className={k.status === "active" ? "text-emerald-500" : "text-zinc-600"}>{k.status}</span>
                          {k.status === "active" && (
                            <button
                              disabled={busy}
                              onClick={() => revokeKey(t.tenant_id, k.id)}
                              className="px-1 py-0.5 border border-rose-500/30 text-rose-400 text-[9px] hover:bg-rose-500/10"
                            >
                              revoke
                            </button>
                          )}
                        </span>
                      </div>
                    ))}
                    <button
                      disabled={busy}
                      onClick={() => createKey(t.tenant_id)}
                      className="mt-1 px-2 py-0.5 border border-amber-500/40 text-amber-400 text-[9px] hover:bg-amber-500/10 disabled:opacity-40"
                    >
                      + generate key
                    </button>
                  </div>
                )}
              </div>
            ))
          )}
        </div>
        {/* New tenant */}
        <div className="flex items-center gap-1 px-2 py-1.5 border-t border-zinc-800">
          <input
            value={newId}
            onChange={(e) => setNewId(e.target.value)}
            placeholder="tenant_id"
            className="bg-zinc-950 border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-300 w-28 outline-none focus:border-amber-500/50"
          />
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="display name"
            className="bg-zinc-950 border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-300 flex-1 outline-none focus:border-amber-500/50"
          />
          <button
            disabled={busy || !newId}
            onClick={createTenant}
            className="px-2 py-0.5 border border-amber-500/40 text-amber-400 text-[10px] hover:bg-amber-500/10 disabled:opacity-40"
          >
            create
          </button>
        </div>
        {revealed && (
          <div className="px-2 py-1.5 border-t border-amber-500/30 bg-amber-500/5">
            <div className="text-[9px] text-amber-400/80 mb-0.5">⚠ copy now — shown once:</div>
            <code className="text-[10px] text-amber-300 break-all select-all">{revealed}</code>
          </div>
        )}
        {msg && <div className="px-2 py-1 border-t border-zinc-800 text-[10px] text-zinc-400">{msg}</div>}
      </div>
    </div>
  );
}
