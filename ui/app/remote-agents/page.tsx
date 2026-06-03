"use client";

import {
  useEffect, useState, useCallback, useRef, useTransition
} from "react";
import { Sidebar } from "@/components/sidebar";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Server, Wifi, WifiOff, RefreshCw, ChevronDown, ChevronUp,
  Copy, Check, Terminal, Activity, HardDrive, Cpu, MemoryStick,
  Clock, Zap, AlertTriangle, CircleHelp, MoreVertical, RotateCcw,
  Power, PowerOff, Trash2, Settings2, FileText, X, CheckCircle2,
  XCircle, Loader2, MonitorCheck, Eye, EyeOff, Save, ArrowUpCircle,
} from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Metrics {
  cpu_percent?: number;
  mem_percent?: number;
  disk_percent?: number;
  load_avg_1m?: number;
  mem_used_mb?: number;
  mem_total_mb?: number;
  ts?: string;
}

interface RemoteAgent {
  agent_id: string;
  hostname: string;
  version: string;
  capabilities: string[];
  platform: string;
  registered_at: number;
  last_seen: number;
  age_seconds: number;
  online: boolean;
  status: "online" | "offline";
  evidence_count: number;
  metrics: Metrics | null;
  eps: number;
  // server_ip stored in hostname or separately
  server_ip?: string;
}

interface AgentsResponse {
  generated_at: string;
  count: number;
  online: number;
  agents: RemoteAgent[];
  source: "gateway" | "mock";
}

interface LogEntry {
  ts: string;
  probe: string;
  result: "PASSED" | "FAILED" | "INCONCLUSIVE" | "SKIPPED";
  alert_hint: string;
  extracted_fact: Record<string, unknown>;
}

interface ProvisionStep {
  ts: number;
  step: string;
  status: "running" | "ok" | "error";
  detail: string;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function fmtEps(eps: number): string {
  if (eps === 0) return "0";
  if (eps < 0.01) return "<0.01";
  return eps.toFixed(2);
}

function fmtTs(ts: string): string {
  const n = parseInt(ts, 10);
  if (!isNaN(n) && n > 1_000_000_000) return new Date(n * 1000).toLocaleTimeString();
  try { return new Date(ts).toLocaleTimeString(); } catch { return ts; }
}

// ── MiniBar ───────────────────────────────────────────────────────────────────

function MiniBar({ label, value, icon: Icon, warn = 75, danger = 90 }: {
  label: string; value: number | undefined;
  icon: typeof Cpu; warn?: number; danger?: number;
}) {
  const pct = value ?? 0;
  const bar = pct >= danger ? "bg-rose-500" : pct >= warn ? "bg-amber-400" : "bg-emerald-400";
  const text = pct >= danger ? "text-rose-400" : pct >= warn ? "text-amber-400" : "text-emerald-400";
  return (
    <div className="flex items-center gap-2">
      <Icon className="h-3 w-3 shrink-0 text-zinc-600" />
      <div className="flex flex-1 items-center gap-1.5">
        <div className="h-1.5 flex-1 rounded-full bg-zinc-800 overflow-hidden">
          <div className={`h-full rounded-full transition-all ${bar}`} style={{ width: `${Math.min(pct, 100)}%` }} />
        </div>
        <span className={`w-9 text-right font-mono text-[10px] ${value === undefined ? "text-zinc-600" : text}`}>
          {value === undefined ? "—" : `${Math.round(pct)}%`}
        </span>
      </div>
      <span className="w-8 text-[9px] uppercase tracking-wide text-zinc-600">{label}</span>
    </div>
  );
}

// ── CopyButton ────────────────────────────────────────────────────────────────

function CopyButton({ text, className = "" }: { text: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const t = useRef<ReturnType<typeof setTimeout> | null>(null);
  return (
    <button onClick={() => {
      navigator.clipboard.writeText(text).catch(() => {});
      setCopied(true);
      if (t.current) clearTimeout(t.current);
      t.current = setTimeout(() => setCopied(false), 2000);
    }} className={`rounded p-1 text-zinc-600 hover:bg-zinc-700 hover:text-zinc-300 transition-colors ${className}`}>
      {copied ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
    </button>
  );
}

// ── LogPanel ──────────────────────────────────────────────────────────────────

function LogPanel({ agentId }: { agentId: string }) {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    fetch(`/api/remote-agents/logs?agent_id=${encodeURIComponent(agentId)}&n=50`, { cache: "no-store" })
      .then(r => r.json()).then(d => { setLogs(d.logs ?? []); setLoading(false); })
      .catch(() => setLoading(false));
  }, [agentId]);

  if (loading) return (
    <div className="mt-3 space-y-1 border-t border-zinc-800/60 pt-3">
      {Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-4 w-full bg-zinc-800/60" />)}
    </div>
  );

  return (
    <div className="mt-3 border-t border-zinc-800/60 pt-3">
      <p className="text-[9px] uppercase tracking-wider text-zinc-600 mb-1.5">
        Log Evidence ({logs.length})
      </p>
      {logs.length === 0
        ? <p className="text-[11px] text-zinc-600 italic">No log evidence yet.</p>
        : (
          <div className="max-h-48 overflow-y-auto space-y-0.5 pr-1">
            {logs.map((e, i) => (
              <div key={i} className="flex items-start gap-2 rounded px-2 py-1 text-[10px] font-mono hover:bg-zinc-800/40">
                <span className="shrink-0 text-zinc-600">{fmtTs(e.ts)}</span>
                <span className={`shrink-0 w-14 font-semibold ${e.result === "FAILED" ? "text-rose-400" : "text-emerald-400"}`}>
                  {e.result}
                </span>
                <span className="text-zinc-400 truncate">{e.alert_hint || e.probe}</span>
              </div>
            ))}
          </div>
        )
      }
    </div>
  );
}

// ── LiveLogsModal ─────────────────────────────────────────────────────────────

function LiveLogsModal({ agentId, serverIp, onClose }: { agentId: string; serverIp: string; onClose: () => void }) {
  const [lines, setLines] = useState<string[]>([]);
  const [paused, setPaused] = useState(false);
  const [filter, setFilter] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const esRef = useRef<EventSource | null>(null);
  const pausedRef = useRef(false);
  pausedRef.current = paused;

  useEffect(() => {
    const url = `/api/remote-agents/${encodeURIComponent(agentId)}/journal?server_ip=${encodeURIComponent(serverIp)}`;
    const es = new EventSource(url);
    esRef.current = es;
    es.onmessage = (e) => {
      if (pausedRef.current) return;
      try {
        const { line } = JSON.parse(e.data) as { line: string };
        setLines(prev => [...prev.slice(-500), line]);
      } catch { /* ignore */ }
    };
    return () => { es.close(); };
  }, [agentId, serverIp]);

  useEffect(() => {
    if (!paused) bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [lines, paused]);

  const filtered = filter ? lines.filter(l => l.toLowerCase().includes(filter.toLowerCase())) : lines;

  const download = () => {
    const blob = new Blob([lines.join("\n")], { type: "text/plain" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
    a.download = `${agentId}-journal.log`; a.click();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div className="flex h-[80vh] w-full max-w-4xl flex-col rounded-xl border border-zinc-700 bg-zinc-950 shadow-2xl"
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
          <div className="flex items-center gap-2">
            <FileText className="h-4 w-4 text-cyan-400" />
            <span className="text-sm font-semibold text-zinc-100">Live Logs — {agentId}</span>
            <span className="text-[10px] text-zinc-500">{serverIp}</span>
          </div>
          <div className="flex items-center gap-2">
            <input value={filter} onChange={e => setFilter(e.target.value)} placeholder="filter..."
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-[11px] text-zinc-300 placeholder-zinc-600 focus:border-cyan-500/50 focus:outline-none w-40" />
            <button onClick={download} title="Download" className="rounded p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300">
              <Copy className="h-3.5 w-3.5" />
            </button>
            <button onClick={() => setPaused(p => !p)}
              className={`rounded px-2 py-1 text-[10px] font-medium transition-colors ${paused ? "bg-amber-500/10 text-amber-400" : "text-zinc-500 hover:bg-zinc-800"}`}>
              {paused ? <Eye className="inline h-3 w-3 mr-1" /> : <EyeOff className="inline h-3 w-3 mr-1" />}
              {paused ? "Resume" : "Pause"}
            </button>
            <button onClick={onClose} className="rounded p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto p-3 font-mono text-[11px] leading-relaxed text-zinc-300 bg-zinc-950">
          {filtered.map((l, i) => (
            <div key={i} className={`hover:bg-zinc-900/40 px-1 py-0.5 ${l.includes("ERROR") || l.includes("FAILED") ? "text-rose-400" : l.includes("WARNING") ? "text-amber-400" : ""}`}>
              {l}
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
        <div className="border-t border-zinc-800 px-4 py-2 text-[10px] text-zinc-600">
          {lines.length} lines · {paused ? "PAUSED" : "streaming"} · filter: {filter || "none"}
        </div>
      </div>
    </div>
  );
}

// ── ConfigModal ───────────────────────────────────────────────────────────────

function ConfigModal({ agentId, serverIp, onClose }: { agentId: string; serverIp: string; onClose: () => void }) {
  const [config, setConfig] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    fetch(`/api/remote-agents/${encodeURIComponent(agentId)}/config?server_ip=${encodeURIComponent(serverIp)}`)
      .then(r => r.json()).then(d => { setConfig(d.config ?? {}); setLoading(false); })
      .catch(() => setLoading(false));
  }, [agentId, serverIp]);

  const save = async () => {
    setSaving(true);
    try {
      await fetch(`/api/remote-agents/${encodeURIComponent(agentId)}/config?server_ip=${encodeURIComponent(serverIp)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config, restart: true }),
      });
      setSaved(true); setTimeout(() => { setSaved(false); onClose(); }, 1500);
    } finally { setSaving(false); }
  };

  const EDITABLE_KEYS = [
    "OMNI_AGENT_COLLECT_INTERVAL",
    "OMNI_AGENT_LOG_PATHS",
    "OMNI_AGENT_K8S_ENABLED",
    "OMNI_AGENT_NAMESPACE",
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div className="w-full max-w-lg rounded-xl border border-zinc-700 bg-zinc-950 shadow-2xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
          <div className="flex items-center gap-2">
            <Settings2 className="h-4 w-4 text-cyan-400" />
            <span className="text-sm font-semibold text-zinc-100">Config — {agentId}</span>
          </div>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300"><X className="h-4 w-4" /></button>
        </div>
        <div className="p-4 space-y-3">
          {loading ? <Skeleton className="h-40 bg-zinc-800" /> : (
            <>
              {EDITABLE_KEYS.map(key => (
                <div key={key}>
                  <label className="text-[10px] uppercase tracking-wider text-zinc-500">{key.replace("OMNI_AGENT_", "")}</label>
                  <input value={config[key] ?? ""} onChange={e => setConfig(c => ({ ...c, [key]: e.target.value }))}
                    className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-200 focus:border-cyan-500/50 focus:outline-none" />
                </div>
              ))}
              <div className="rounded border border-zinc-800 bg-zinc-900/50 p-3">
                <p className="text-[10px] uppercase tracking-wider text-zinc-600 mb-2">All env vars</p>
                <div className="max-h-32 overflow-y-auto font-mono text-[10px] text-zinc-500 space-y-0.5">
                  {Object.entries(config).map(([k, v]) => (
                    <div key={k}><span className="text-zinc-400">{k}</span>=<span className="text-zinc-500">{v}</span></div>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
        <div className="flex justify-end gap-2 border-t border-zinc-800 px-4 py-3">
          <button onClick={onClose} className="rounded px-3 py-1.5 text-sm text-zinc-400 hover:bg-zinc-800">Cancel</button>
          <button onClick={save} disabled={saving || saved}
            className="flex items-center gap-1.5 rounded bg-cyan-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-cyan-500 disabled:opacity-50">
            {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : saved ? <CheckCircle2 className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />}
            {saved ? "Saved!" : "Save & Restart"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── UpdateModal ───────────────────────────────────────────────────────────────

function UpdateModal({ agent, onClose, onDone }: {
  agent: RemoteAgent;
  onClose: () => void;
  onDone: (ok: boolean, msg: string) => void;
}) {
  const [version, setVersion] = useState("1.1.0");
  const [downloadUrl, setDownloadUrl] = useState("");
  const [sha256, setSha256] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (!version || !downloadUrl || !sha256) return;
    setSubmitting(true);
    try {
      const res = await fetch(
        `/api/remote-agents/${encodeURIComponent(agent.agent_id)}/action?action=update`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ version, download_url: downloadUrl, sha256_checksum: sha256 }),
        },
      );
      const data = await res.json() as { status?: string; error?: string; detail?: string };
      if (res.ok && data.status === "enqueued") {
        onDone(true, `Update v${version} enqueued — agent will update within ~60s`);
      } else {
        onDone(false, data.error ?? data.detail ?? "Gateway rejected the request");
      }
    } catch (err: unknown) {
      onDone(false, String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div className="w-full max-w-lg rounded-xl border border-zinc-700 bg-zinc-950 shadow-2xl"
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
          <div className="flex items-center gap-2">
            <ArrowUpCircle className="h-4 w-4 text-cyan-400" />
            <span className="text-sm font-semibold text-zinc-100">Update Agent — {agent.agent_id}</span>
            <span className="text-[10px] text-zinc-500">current: v{agent.version}</span>
          </div>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300"><X className="h-4 w-4" /></button>
        </div>

        <div className="p-4 space-y-3">
          <div className="rounded border border-amber-500/20 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-400 flex items-start gap-2">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
            Agent sẽ download, verify SHA-256, replace binary và restart via systemctl. Cần OMNI_AGENT_UPDATE_ALLOWED_HOSTS có cấu hình.
          </div>

          <div className="space-y-1.5">
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">Version *</label>
            <input value={version} onChange={e => setVersion(e.target.value)} placeholder="1.1.0"
              className="w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-200 focus:border-cyan-500/50 focus:outline-none" />
          </div>

          <div className="space-y-1.5">
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">Download URL * (https only)</label>
            <input value={downloadUrl} onChange={e => setDownloadUrl(e.target.value)}
              placeholder="https://releases.example.com/omni-agent-1.1.0.tar.gz"
              className="w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-xs text-zinc-200 placeholder-zinc-700 focus:border-cyan-500/50 focus:outline-none" />
          </div>

          <div className="space-y-1.5">
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">SHA-256 Checksum *</label>
            <input value={sha256} onChange={e => setSha256(e.target.value)}
              placeholder="abc123def456..."
              className="w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-xs text-zinc-200 placeholder-zinc-700 focus:border-cyan-500/50 focus:outline-none" />
          </div>
        </div>

        <div className="flex justify-end gap-2 border-t border-zinc-800 px-4 py-3">
          <button onClick={onClose} className="rounded px-3 py-1.5 text-sm text-zinc-400 hover:bg-zinc-800">Cancel</button>
          <button onClick={submit}
            disabled={submitting || !version || !downloadUrl || !sha256}
            className="flex items-center gap-1.5 rounded bg-cyan-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-cyan-500 disabled:opacity-50">
            {submitting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ArrowUpCircle className="h-3.5 w-3.5" />}
            Enqueue Update
          </button>
        </div>
      </div>
    </div>
  );
}

// ── AgentMenu ─────────────────────────────────────────────────────────────────

function AgentMenu({ agent, serverIp, onDeregistered, onOpenLogs, onOpenConfig, onOpenUpdate }: {
  agent: RemoteAgent;
  serverIp: string;
  onDeregistered: () => void;
  onOpenLogs: () => void;
  onOpenConfig: () => void;
  onOpenUpdate: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<"deregister" | "uninstall" | null>(null);
  const [toast, setToast] = useState<{ msg: string; ok: boolean } | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const showToast = (msg: string, ok: boolean) => {
    setToast({ msg, ok }); setTimeout(() => setToast(null), 3000);
  };

  const doAction = async (action: string) => {
    setPending(action); setOpen(false);
    try {
      const url = `/api/remote-agents/${encodeURIComponent(agent.agent_id)}/action?action=${action}&server_ip=${encodeURIComponent(serverIp)}`;
      const res = await fetch(url, { method: "POST" });
      const data = await res.json();
      if (action === "deregister") { onDeregistered(); showToast("Deregistered", true); return; }
      showToast(data.ok ? `${action} OK` : (data.stderr || "failed"), !!data.ok);
    } catch { showToast("Provisioner unreachable", false); }
    finally { setPending(null); }
  };

  const MenuItem = ({ icon: Icon, label, color = "", onClick: handler }: {
    icon: typeof RotateCcw; label: string; color?: string; onClick: () => void;
  }) => (
    <button onClick={handler}
      className={`flex w-full items-center gap-2.5 px-3 py-2 text-xs transition-colors hover:bg-zinc-800 ${color || "text-zinc-300"}`}>
      <Icon className="h-3.5 w-3.5 shrink-0" />{label}
    </button>
  );

  return (
    <div className="relative" ref={ref}>
      {toast && (
        <div className={`absolute right-0 top-8 z-20 whitespace-nowrap rounded px-2.5 py-1.5 text-[11px] font-medium shadow ${toast.ok ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20" : "bg-rose-500/10 text-rose-400 border border-rose-500/20"}`}>
          {toast.msg}
        </div>
      )}

      <button onClick={() => setOpen(o => !o)}
        className="rounded p-1 text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300 transition-colors">
        {pending ? <Loader2 className="h-4 w-4 animate-spin text-cyan-400" /> : <MoreVertical className="h-4 w-4" />}
      </button>

      {open && (
        <div className="absolute right-0 top-7 z-20 w-44 rounded-lg border border-zinc-700 bg-zinc-900 py-1 shadow-xl">
          <MenuItem icon={RotateCcw} label="Restart" onClick={() => doAction("restart")} />
          <MenuItem icon={Power} label="Enable service" onClick={() => doAction("enable")} />
          <MenuItem icon={PowerOff} label="Disable service" onClick={() => doAction("disable")} />
          <div className="my-1 border-t border-zinc-800" />
          <MenuItem icon={ArrowUpCircle} label="Update Agent" color="text-cyan-400"
            onClick={() => { setOpen(false); onOpenUpdate(); }} />
          <div className="my-1 border-t border-zinc-800" />
          <MenuItem icon={FileText} label="Live Logs" onClick={() => { setOpen(false); onOpenLogs(); }} />
          <MenuItem icon={Settings2} label="Edit Config" onClick={() => { setOpen(false); onOpenConfig(); }} />
          <div className="my-1 border-t border-zinc-800" />
          <MenuItem icon={XCircle} label="Deregister" color="text-amber-400"
            onClick={() => { setOpen(false); setConfirm("deregister"); }} />
          <MenuItem icon={Trash2} label="Uninstall" color="text-rose-400"
            onClick={() => { setOpen(false); setConfirm("uninstall"); }} />
        </div>
      )}

      {confirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
          onClick={() => setConfirm(null)}>
          <div className="w-80 rounded-xl border border-zinc-700 bg-zinc-950 p-5 shadow-2xl" onClick={e => e.stopPropagation()}>
            <p className="text-sm font-semibold text-zinc-100 mb-1">
              {confirm === "uninstall" ? "Uninstall agent?" : "Deregister agent?"}
            </p>
            <p className="text-xs text-zinc-500 mb-4">
              {confirm === "uninstall"
                ? `This will remove omni-agent from ${serverIp} and delete the SSH tunnel.`
                : `Agent ${agent.agent_id} will be removed from the fleet registry.`}
            </p>
            <div className="flex gap-2 justify-end">
              <button onClick={() => setConfirm(null)} className="rounded px-3 py-1.5 text-sm text-zinc-400 hover:bg-zinc-800">Cancel</button>
              <button onClick={() => { setConfirm(null); doAction(confirm); }}
                className="rounded bg-rose-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-500">
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── AgentCard ─────────────────────────────────────────────────────────────────

function AgentCard({ agent, onDeregistered }: { agent: RemoteAgent; onDeregistered: () => void }) {
  const [logsOpen, setLogsOpen] = useState(false);
  const [showLiveLogs, setShowLiveLogs] = useState(false);
  const [showConfig, setShowConfig] = useState(false);
  const [showUpdate, setShowUpdate] = useState(false);
  const [updateToast, setUpdateToast] = useState<{ msg: string; ok: boolean } | null>(null);

  const serverIp = agent.server_ip || agent.hostname;
  const m = agent.metrics;

  const handleUpdateDone = (ok: boolean, msg: string) => {
    setShowUpdate(false);
    setUpdateToast({ msg, ok });
    setTimeout(() => setUpdateToast(null), 5000);
  };

  return (
    <>
      {showLiveLogs && (
        <LiveLogsModal agentId={agent.agent_id} serverIp={serverIp} onClose={() => setShowLiveLogs(false)} />
      )}
      {showConfig && (
        <ConfigModal agentId={agent.agent_id} serverIp={serverIp} onClose={() => setShowConfig(false)} />
      )}
      {showUpdate && (
        <UpdateModal agent={agent} onClose={() => setShowUpdate(false)} onDone={handleUpdateDone} />
      )}

      <div className={`rounded-lg border p-4 transition-all ${agent.online ? "border-emerald-500/30 bg-emerald-500/5" : "border-zinc-700 bg-zinc-900/40"}`}>
        {/* Header */}
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2.5 min-w-0">
            <div className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-md ${agent.online ? "bg-emerald-500/10" : "bg-zinc-800"}`}>
              {agent.online ? <Wifi className="h-4 w-4 text-emerald-400" /> : <WifiOff className="h-4 w-4 text-zinc-500" />}
            </div>
            <div className="min-w-0">
              <p className="font-mono text-sm font-semibold text-zinc-100 truncate">{agent.agent_id}</p>
              <p className="text-[10px] text-zinc-500 truncate">{agent.hostname}</p>
            </div>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {updateToast && (
              <span className={`mr-1 rounded px-2 py-0.5 text-[9px] font-medium ${updateToast.ok ? "bg-cyan-500/10 text-cyan-400" : "bg-rose-500/10 text-rose-400"}`}>
                {updateToast.msg}
              </span>
            )}
            <span className={`rounded px-1.5 py-0.5 text-[9px] font-bold uppercase ${agent.online ? "bg-emerald-500/10 text-emerald-400" : "bg-zinc-800 text-zinc-500"}`}>
              {agent.status}
            </span>
            <AgentMenu agent={agent} serverIp={serverIp} onDeregistered={onDeregistered}
              onOpenLogs={() => setShowLiveLogs(true)} onOpenConfig={() => setShowConfig(true)}
              onOpenUpdate={() => setShowUpdate(true)} />
          </div>
        </div>

        {/* Metrics */}
        <div className="mt-3 space-y-1.5">
          <MiniBar label="CPU" value={m?.cpu_percent} icon={Cpu} />
          <MiniBar label="Mem" value={m?.mem_percent} icon={MemoryStick} />
          <MiniBar label="Disk" value={m?.disk_percent} icon={HardDrive} />
        </div>
        {m?.load_avg_1m !== undefined && (
          <p className="mt-1 text-[9px] text-zinc-600 font-mono">
            load: {m.load_avg_1m?.toFixed(2)} · {m.mem_used_mb?.toFixed(0)}MB/{m.mem_total_mb?.toFixed(0)}MB
          </p>
        )}

        {/* Stats row */}
        <div className="mt-3 grid grid-cols-3 gap-1.5">
          <div className="rounded bg-zinc-950/50 px-2 py-1.5">
            <p className="text-[9px] uppercase tracking-wider text-zinc-600">EPS</p>
            <div className="flex items-center gap-1 mt-0.5">
              <Zap className="h-2.5 w-2.5 text-cyan-500" />
              <p className="font-mono text-[11px] text-cyan-400">{fmtEps(agent.eps)}/s</p>
            </div>
          </div>
          <div className="rounded bg-zinc-950/50 px-2 py-1.5">
            <p className="text-[9px] uppercase tracking-wider text-zinc-600">Last Seen</p>
            <div className="flex items-center gap-1 mt-0.5">
              <Clock className="h-2.5 w-2.5 text-zinc-600" />
              <p className={`font-mono text-[11px] ${agent.age_seconds > 90 ? "text-amber-400" : "text-zinc-300"}`}>
                {fmtAge(agent.age_seconds)}
              </p>
            </div>
          </div>
          <div className="rounded bg-zinc-950/50 px-2 py-1.5">
            <p className="text-[9px] uppercase tracking-wider text-zinc-600">Events</p>
            <p className="font-mono text-[11px] text-zinc-300 mt-0.5">{agent.evidence_count ?? 0}</p>
          </div>
        </div>

        {/* Capabilities + version */}
        <div className="mt-2.5 flex items-center gap-1.5 flex-wrap">
          {agent.capabilities?.map(c => (
            <span key={c} className={`rounded-sm px-1.5 py-0.5 text-[9px] uppercase tracking-wide ${c === "discovery" ? "bg-cyan-500/10 text-cyan-500" : "bg-zinc-800 text-zinc-500"}`}>{c}</span>
          ))}
          <span className="ml-auto rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[9px] font-semibold text-cyan-400">
            v{agent.version}
          </span>
        </div>

        {/* Log evidence toggle */}
        <button onClick={() => setLogsOpen(o => !o)}
          className="mt-3 flex w-full items-center justify-between rounded px-2 py-1.5 text-[10px] text-zinc-500 hover:bg-zinc-800/50 hover:text-zinc-300 transition-colors">
          <span className="flex items-center gap-1"><Terminal className="h-3 w-3" />Log Evidence</span>
          {logsOpen ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        </button>
        {logsOpen && <LogPanel agentId={agent.agent_id} />}
      </div>
    </>
  );
}

// ── ProvisionProgress ─────────────────────────────────────────────────────────

const STEP_LABELS: Record<string, string> = {
  ssh_check: "SSH connectivity",
  tunnel: "SSH tunnel LaunchAgent",
  tunnel_verify: "Verify tunnel port",
  venv_archive: "Archive venv from .86",
  venv_download: "Download venv",
  upload: "Upload files to server",
  extract: "Extract & setup",
  install: "Run install.sh",
  verify_service: "Verify service",
  done: "Complete",
};

function ProvisionProgress({ taskId, onDone }: { taskId: string; onDone: (ok: boolean) => void }) {
  const [steps, setSteps] = useState<ProvisionStep[]>([]);
  const [ended, setEnded] = useState(false);

  useEffect(() => {
    const es = new EventSource(`/api/remote-agents/provision?task_id=${taskId}`);
    es.onmessage = (e) => {
      try {
        const step = JSON.parse(e.data) as ProvisionStep & { step: string };
        if (step.step === "__end__") {
          setEnded(true); es.close();
          onDone(step.status === "ok");
          return;
        }
        setSteps(prev => {
          const existing = prev.findIndex(s => s.step === step.step);
          if (existing >= 0) { const n = [...prev]; n[existing] = step; return n; }
          return [...prev, step];
        });
      } catch { /* ignore */ }
    };
    return () => es.close();
  }, [taskId, onDone]);

  return (
    <div className="mt-4 space-y-1.5">
      {steps.map((s, i) => (
        <div key={i} className="flex items-start gap-2.5 text-sm">
          <span className="mt-0.5 shrink-0">
            {s.status === "running" && <Loader2 className="h-4 w-4 animate-spin text-cyan-400" />}
            {s.status === "ok" && <CheckCircle2 className="h-4 w-4 text-emerald-400" />}
            {s.status === "error" && <XCircle className="h-4 w-4 text-rose-400" />}
          </span>
          <div className="min-w-0">
            <p className="text-zinc-200">{STEP_LABELS[s.step] ?? s.step}</p>
            {s.detail && <p className="font-mono text-[10px] text-zinc-500 truncate">{s.detail}</p>}
          </div>
        </div>
      ))}
      {!ended && steps.length === 0 && (
        <p className="text-sm text-zinc-500 flex items-center gap-2">
          <Loader2 className="h-4 w-4 animate-spin" /> Connecting...
        </p>
      )}
    </div>
  );
}

// ── InstallWizard ─────────────────────────────────────────────────────────────

function InstallWizard({ onInstalled }: { onInstalled: () => void }) {
  const [serverIp, setServerIp] = useState("");
  const [agentId, setAgentId] = useState("");
  const [logPaths, setLogPaths] = useState("/var/log/syslog,/var/log/auth.log");
  const [taskId, setTaskId] = useState<string | null>(null);
  const [done, setDone] = useState<boolean | null>(null);
  const [, startTransition] = useTransition();

  const derivedId = agentId || (serverIp ? `agent-${serverIp.split(".").pop()}` : "");

  const startInstall = async () => {
    if (!serverIp.trim()) return;
    setDone(null);
    try {
      const res = await fetch("/api/remote-agents/provision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ server_ip: serverIp.trim(), agent_id: derivedId, log_paths: logPaths, no_k8s: true }),
      });
      const data = await res.json();
      if (data.error) { alert(data.error); return; }
      setTaskId(data.task_id);
    } catch (err) { alert(String(err)); }
  };

  const handleDone = useCallback((ok: boolean) => {
    setDone(ok);
    if (ok) { startTransition(() => { setTimeout(onInstalled, 2000); }); }
  }, [onInstalled]);

  if (taskId) {
    return (
      <div className="space-y-4">
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-5">
          <div className="flex items-center justify-between mb-4">
            <div>
              <p className="text-sm font-semibold text-zinc-200">Installing on {serverIp}</p>
              <p className="text-[11px] text-zinc-500">Agent ID: {derivedId}</p>
            </div>
            {done !== null && (
              <span className={`flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-semibold ${done ? "bg-emerald-500/10 text-emerald-400" : "bg-rose-500/10 text-rose-400"}`}>
                {done ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
                {done ? "Success" : "Failed"}
              </span>
            )}
          </div>
          <ProvisionProgress taskId={taskId} onDone={handleDone} />
        </div>
        {done !== null && (
          <button onClick={() => { setTaskId(null); setDone(null); setServerIp(""); setAgentId(""); }}
            className="text-sm text-zinc-500 hover:text-zinc-300 transition-colors">
            ← Install another agent
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-lg">
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-5 space-y-4">
        <p className="text-sm font-semibold text-zinc-200 flex items-center gap-2">
          <MonitorCheck className="h-4 w-4 text-cyan-400" />
          New Agent Setup
        </p>

        <div className="space-y-1.5">
          <label className="text-[10px] uppercase tracking-wider text-zinc-500">Server IP *</label>
          <input type="text" placeholder="10.210.14.xxx" value={serverIp}
            onChange={e => setServerIp(e.target.value)}
            className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 font-mono text-sm text-zinc-200 placeholder-zinc-700 focus:border-cyan-500/50 focus:outline-none" />
        </div>

        <div className="space-y-1.5">
          <label className="text-[10px] uppercase tracking-wider text-zinc-500">Agent ID</label>
          <input type="text" placeholder={derivedId || "auto from IP"} value={agentId}
            onChange={e => setAgentId(e.target.value)}
            className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 font-mono text-sm text-zinc-200 placeholder-zinc-700 focus:border-cyan-500/50 focus:outline-none" />
        </div>

        <div className="space-y-1.5">
          <label className="text-[10px] uppercase tracking-wider text-zinc-500">Log Paths</label>
          <input type="text" value={logPaths} onChange={e => setLogPaths(e.target.value)}
            className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 font-mono text-xs text-zinc-200 focus:border-cyan-500/50 focus:outline-none" />
        </div>

        <div className="rounded border border-amber-500/20 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-400 flex items-start gap-2">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          Mọi cài đặt đi qua 10.210.14.86 làm local repo (offline). Provisioner daemon phải đang chạy trên Mac.
        </div>

        <button onClick={startInstall} disabled={!serverIp.trim()}
          className="w-full rounded bg-cyan-600 py-2.5 text-sm font-semibold text-white hover:bg-cyan-500 disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2">
          <Server className="h-4 w-4" />
          Install Agent on {serverIp || "..."}
        </button>
      </div>

      <div className="rounded border border-zinc-800 px-4 py-3 text-[11px] text-zinc-500">
        <p className="font-semibold text-zinc-400 mb-1 flex items-center gap-1">
          <CircleHelp className="h-3 w-3" /> Provisioner không chạy?
        </p>
        <p>Khởi động lại LaunchAgent:</p>
        <div className="flex items-center gap-2 mt-1">
          <code className="font-mono text-zinc-400 text-[10px]">launchctl start com.omni.provisioner</code>
          <CopyButton text="launchctl start com.omni.provisioner" />
        </div>
      </div>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function RemoteAgentsPage() {
  const [data, setData] = useState<AgentsResponse | null>(null);
  const [lastRefresh, setLastRefresh] = useState(new Date());
  const [activeTab, setActiveTab] = useState("fleet");

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/remote-agents", { cache: "no-store" });
      if (res.ok) { setData(await res.json()); setLastRefresh(new Date()); }
    } catch { /* keep existing */ }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 10_000);
    return () => clearInterval(t);
  }, [load]);

  const handleDeregistered = useCallback(() => { setTimeout(load, 500); }, [load]);
  const handleInstalled = useCallback(() => { setActiveTab("fleet"); setTimeout(load, 2000); }, [load]);

  const isLive = data?.source === "gateway";

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        {/* Header */}
        <header className="sticky top-0 z-10 flex h-16 items-center justify-between border-b border-zinc-800 bg-zinc-950/80 px-6 backdrop-blur">
          <div className="flex items-center gap-3">
            <h1 className="text-base font-semibold text-zinc-100">Remote Agents</h1>
            <span className="flex h-5 items-center gap-1 rounded-full bg-emerald-500/10 px-2 text-[10px] font-semibold text-emerald-400 ring-1 ring-emerald-500/20">
              <Wifi className="h-2.5 w-2.5" />{data?.online ?? "—"}/{data?.count ?? "—"} online
            </span>
          </div>
          <div className="flex items-center gap-3 text-xs text-zinc-600">
            <span className="flex items-center gap-1">
              <span className={`h-1.5 w-1.5 rounded-full ${isLive ? "bg-emerald-400" : "bg-amber-400"}`} />
              {isLive ? "live" : "mock"}
            </span>
            <button onClick={load} className="flex items-center gap-1 hover:text-zinc-300 transition-colors">
              <RefreshCw className="h-3 w-3" />{lastRefresh.toLocaleTimeString()}
            </button>
          </div>
        </header>

        <div className="p-6">
          <Tabs value={activeTab} onValueChange={setActiveTab}>
            <TabsList className="mb-6 bg-zinc-900 border border-zinc-800">
              <TabsTrigger value="fleet" className="data-[state=active]:bg-zinc-800 data-[state=active]:text-zinc-100">
                <Server className="mr-1.5 h-3.5 w-3.5" />Fleet ({data?.count ?? 0})
              </TabsTrigger>
              <TabsTrigger value="install" className="data-[state=active]:bg-zinc-800 data-[state=active]:text-zinc-100">
                <Terminal className="mr-1.5 h-3.5 w-3.5" />Install Agent
              </TabsTrigger>
            </TabsList>

            <TabsContent value="fleet">
              {!data ? (
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
                  {Array.from({ length: 3 }).map((_, i) => (
                    <div key={i} className="rounded-lg border border-zinc-800 p-4">
                      <Skeleton className="h-52 w-full bg-zinc-800" />
                    </div>
                  ))}
                </div>
              ) : data.agents.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-24 text-center">
                  <Activity className="h-10 w-10 text-zinc-700 mb-3" />
                  <p className="text-sm font-medium text-zinc-400">No remote agents registered</p>
                  <button onClick={() => setActiveTab("install")}
                    className="mt-3 text-xs text-cyan-400 hover:text-cyan-300 underline-offset-2 underline">
                    Install first agent →
                  </button>
                </div>
              ) : (
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
                  {data.agents.map(agent => (
                    <AgentCard key={agent.agent_id} agent={agent} onDeregistered={handleDeregistered} />
                  ))}
                </div>
              )}
            </TabsContent>

            <TabsContent value="install">
              <InstallWizard onInstalled={handleInstalled} />
            </TabsContent>
          </Tabs>
        </div>
      </main>
    </div>
  );
}
