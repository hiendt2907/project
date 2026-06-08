"use client";

import { useCallback, useEffect, useState } from "react";
import { SectionLabel, Unavailable } from "@/components/shared/primitives";

// Runtime Flags — typed key/value config persisted to PostgreSQL (MASTER_PLAN §6.7).
// bool → toggle; int/float → number; str/json → text. Source shown (DB).

type ValueType = "bool" | "int" | "float" | "str" | "json";

interface FlagRow {
  flag_key: string;
  flag_value: unknown;
  value_type: ValueType;
  updated_by: string;
  updated_at: string | null;
  version: number;
}

interface RuntimeFlagsPanelProps {
  tenant: string;
}

const TYPES: ValueType[] = ["bool", "int", "float", "str", "json"];

export function RuntimeFlagsPanel({ tenant }: RuntimeFlagsPanelProps) {
  const [flags, setFlags] = useState<FlagRow[] | null>(null);
  const [loadErr, setLoadErr] = useState(false);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [nk, setNk] = useState("");
  const [nv, setNv] = useState("");
  const [nt, setNt] = useState<ValueType>("str");

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api/autonomy/flags?tenant_id=${encodeURIComponent(tenant)}`, { cache: "no-store" });
      if (!res.ok) return setLoadErr(true);
      const data = (await res.json()) as { flags: FlagRow[] };
      setFlags(data.flags);
      setLoadErr(false);
    } catch {
      setLoadErr(true);
    }
  }, [tenant]);

  useEffect(() => {
    void load();
  }, [load]);

  const coerce = useCallback((raw: string, t: ValueType): unknown => {
    if (t === "bool") return raw === "true" || raw === "1";
    if (t === "int") return parseInt(raw, 10);
    if (t === "float") return parseFloat(raw);
    if (t === "json") return JSON.parse(raw);
    return raw;
  }, []);

  const save = useCallback(
    async (flag_key: string, raw: string, value_type: ValueType) => {
      setBusy(true);
      setMsg("");
      try {
        const flag_value = coerce(raw, value_type);
        const res = await fetch("/api/autonomy/flags", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ flag_key, flag_value, value_type, tenant_id: tenant, actor: "admin_ui" }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) setMsg(`✗ ${data.detail ?? data.error ?? res.status}`);
        else {
          setMsg(`✓ ${flag_key} saved (v${data.version})`);
          setNk("");
          setNv("");
          await load();
        }
      } catch (e) {
        setMsg(`✗ ${e instanceof Error ? e.message : "invalid value"}`);
      } finally {
        setBusy(false);
      }
    },
    [tenant, coerce, load],
  );

  if (loadErr) {
    return (
      <div>
        <SectionLabel text="Runtime Flags" />
        <Unavailable detail="gateway /autonomy/flags unreachable" />
      </div>
    );
  }

  return (
    <div>
      <SectionLabel text="Runtime Flags" note={<span className="text-zinc-600">source: PostgreSQL omni_admin</span>} />
      <div className="border border-zinc-800 bg-zinc-900/40">
        <div className="max-h-56 overflow-auto">
          {flags === null ? (
            <div className="p-3 text-zinc-600 text-[10px]">loading…</div>
          ) : flags.length === 0 ? (
            <div className="p-3 text-zinc-600 text-[10px]">no flags set — add one below</div>
          ) : (
            <table className="w-full text-[10px]">
              <tbody>
                {flags.map((f) => (
                  <FlagEditRow key={f.flag_key} flag={f} busy={busy} onSave={save} />
                ))}
              </tbody>
            </table>
          )}
        </div>
        {/* New flag form */}
        <div className="flex items-center gap-1 px-2 py-1.5 border-t border-zinc-800">
          <input
            value={nk}
            onChange={(e) => setNk(e.target.value)}
            placeholder="flag_key"
            className="bg-zinc-950 border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-300 w-32 outline-none focus:border-amber-500/50"
          />
          <select
            value={nt}
            onChange={(e) => setNt(e.target.value as ValueType)}
            className="bg-zinc-950 border border-zinc-700 px-1 py-0.5 text-[10px] text-zinc-400"
          >
            {TYPES.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
          <input
            value={nv}
            onChange={(e) => setNv(e.target.value)}
            placeholder="value"
            className="bg-zinc-950 border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-300 flex-1 outline-none focus:border-amber-500/50"
          />
          <button
            disabled={busy || !nk}
            onClick={() => save(nk, nv, nt)}
            className="px-2 py-0.5 border border-amber-500/40 text-amber-400 text-[10px] hover:bg-amber-500/10 disabled:opacity-40"
          >
            add
          </button>
        </div>
        {msg && <div className="px-2 py-1 border-t border-zinc-800 text-[10px] text-zinc-400">{msg}</div>}
      </div>
    </div>
  );
}

interface FlagEditRowProps {
  flag: FlagRow;
  busy: boolean;
  onSave: (key: string, raw: string, type: ValueType) => void;
}

function FlagEditRow({ flag, busy, onSave }: FlagEditRowProps) {
  const initial = flag.value_type === "json" ? JSON.stringify(flag.flag_value) : String(flag.flag_value);
  const [val, setVal] = useState(initial);
  const dirty = val !== initial;

  if (flag.value_type === "bool") {
    const on = flag.flag_value === true;
    return (
      <tr className="border-b border-zinc-800/60">
        <td className="px-2 py-1 text-zinc-300">{flag.flag_key}</td>
        <td className="px-2 py-1 text-right">
          <button
            disabled={busy}
            onClick={() => onSave(flag.flag_key, on ? "false" : "true", "bool")}
            className={`px-2 py-0.5 border text-[9px] ${on ? "text-emerald-400 border-emerald-500/40" : "text-zinc-600 border-zinc-700"}`}
          >
            {on ? "ON" : "OFF"}
          </button>
        </td>
      </tr>
    );
  }

  return (
    <tr className="border-b border-zinc-800/60">
      <td className="px-2 py-1 text-zinc-300 whitespace-nowrap">
        {flag.flag_key}
        <span className="ml-1 text-zinc-600">{flag.value_type}</span>
      </td>
      <td className="px-2 py-1 flex items-center gap-1 justify-end">
        <input
          value={val}
          onChange={(e) => setVal(e.target.value)}
          className="bg-zinc-950 border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-300 w-32 outline-none focus:border-amber-500/50"
        />
        <button
          disabled={busy || !dirty}
          onClick={() => onSave(flag.flag_key, val, flag.value_type)}
          className="px-1.5 py-0.5 border border-amber-500/40 text-amber-400 text-[9px] hover:bg-amber-500/10 disabled:opacity-30"
        >
          save
        </button>
      </td>
    </tr>
  );
}
