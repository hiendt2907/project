"use client";

import { useEffect, useState } from "react";
import { Sidebar } from "@/components/sidebar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { ScrollArea } from "@/components/ui/scroll-area";
import { AlertTriangle, AlertCircle, Info, Search, RefreshCw, Clock, Download } from "lucide-react";

type LedgerEntry = {
  id: string;
  level: "error" | "critical" | "warning";
  worker: string;
  message: string;
  trace_id: string;
  timestamp: string;
  ttl_remaining_s: number;
};

const levelConfig = {
  critical: { icon: AlertCircle, color: "text-red-400", bg: "bg-red-500/5 border-red-900/40", badge: "bg-red-500/10 text-red-400 border-red-500/20" },
  error: { icon: AlertTriangle, color: "text-amber-400", bg: "bg-amber-500/5 border-amber-900/40", badge: "bg-amber-500/10 text-amber-400 border-amber-500/20" },
  warning: { icon: Info, color: "text-zinc-400", bg: "bg-zinc-800/30 border-zinc-800", badge: "bg-zinc-700/50 text-zinc-400 border-zinc-700" },
};

function ttlBar(ttl: number) {
  const pct = Math.min(100, Math.round((ttl / 3600) * 100));
  return (
    <div className="flex items-center gap-2">
      <div className="h-1 w-16 rounded-full bg-zinc-800 overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${pct > 50 ? "bg-emerald-500" : pct > 20 ? "bg-amber-500" : "bg-red-500"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="font-mono text-[10px] text-zinc-600">{Math.floor(ttl / 60)}m</span>
    </div>
  );
}

export default function LedgerPage() {
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [levelFilter, setLevelFilter] = useState("all");
  const [workerFilter, setWorkerFilter] = useState("all");
  const [lastRefresh, setLastRefresh] = useState(new Date());

  async function fetchLedger() {
    setFetchError(null);
    try {
      const res = await fetch("/api/ledger");
      if (!res.ok) throw new Error(`API ${res.status}`);
      const data = await res.json();
      setEntries(data.entries ?? []);
      setTotal(data.total ?? 0);
      setLastRefresh(new Date());
    } catch (e: unknown) {
      setFetchError(e instanceof Error ? e.message : "Failed to load ledger");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchLedger();
    const t = setInterval(fetchLedger, 15_000);
    return () => clearInterval(t);
  }, []);

  const workers = [...new Set(entries.map((e) => e.worker))];

  const filtered = entries.filter((e) => {
    if (levelFilter !== "all" && e.level !== levelFilter) return false;
    if (workerFilter !== "all" && e.worker !== workerFilter) return false;
    if (search && !e.message.toLowerCase().includes(search.toLowerCase()) && !e.trace_id.includes(search)) return false;
    return true;
  });

  const counts = { critical: entries.filter((e) => e.level === "critical").length, error: entries.filter((e) => e.level === "error").length, warning: entries.filter((e) => e.level === "warning").length };

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        <header className="sticky top-0 z-10 flex h-16 items-center justify-between border-b border-zinc-800 bg-zinc-950/80 px-6 backdrop-blur">
          <div>
            <h1 className="text-base font-semibold text-zinc-100">Error Ledger</h1>
            <p className="text-xs text-zinc-500">Redis TTL-keyed error log · auto-expires</p>
          </div>
          <div className="flex items-center gap-3 text-xs text-zinc-600">
            <button
              onClick={() => {
                const header = "id,level,worker,message,trace_id,timestamp,ttl_remaining_s\n";
                const rows = filtered
                  .map((e) =>
                    [
                      e.id,
                      e.level,
                      e.worker,
                      `"${e.message.replace(/"/g, '""')}"`,
                      e.trace_id,
                      e.timestamp,
                      e.ttl_remaining_s,
                    ].join(",")
                  )
                  .join("\n");
                const blob = new Blob([header + rows], { type: "text/csv" });
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = `ledger-${new Date().toISOString().slice(0, 10)}.csv`;
                a.click();
                URL.revokeObjectURL(url);
              }}
              className="flex items-center gap-1.5 rounded border border-zinc-700 px-2.5 py-1 text-zinc-400 hover:border-zinc-600 hover:text-zinc-200 transition-colors"
            >
              <Download className="h-3.5 w-3.5" />
              Export CSV
            </button>
            <span className="flex items-center gap-1">
              <RefreshCw className="h-3 w-3" />
              {lastRefresh.toLocaleTimeString()}
            </span>
          </div>
        </header>

        <div className="p-6 space-y-4">
          {fetchError && (
            <div className="flex items-center justify-between rounded-lg border border-red-500/20 bg-red-500/5 px-4 py-3">
              <div className="flex items-center gap-2">
                <AlertCircle className="h-4 w-4 text-red-400" />
                <span className="text-sm text-red-400">{fetchError}</span>
              </div>
              <button onClick={fetchLedger} className="text-xs text-red-400/70 hover:text-red-400 underline">Retry</button>
            </div>
          )}
          <div className="grid grid-cols-3 gap-3">
            {(["critical", "error", "warning"] as const).map((lvl) => {
              const cfg = levelConfig[lvl];
              const Icon = cfg.icon;
              return (
                <Card key={lvl} className="border-zinc-800 bg-zinc-900/60 cursor-pointer hover:border-zinc-700 transition-colors"
                  onClick={() => setLevelFilter(levelFilter === lvl ? "all" : lvl)}>
                  <CardContent className="flex items-center gap-3 p-4">
                    <Icon className={`h-5 w-5 ${cfg.color}`} />
                    <div>
                      <p className="text-xs uppercase tracking-widest text-zinc-500">{lvl}</p>
                      <p className={`text-xl font-bold font-mono ${cfg.color}`}>{counts[lvl]}</p>
                    </div>
                  </CardContent>
                </Card>
              );
            })}
          </div>

          <div className="flex flex-wrap gap-3">
            <div className="relative flex-1 min-w-[200px]">
              <Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search message or trace ID…"
                className="pl-9 border-zinc-800 bg-zinc-900 text-zinc-100 h-9 text-sm"
              />
            </div>
            <Select value={levelFilter} onValueChange={(v) => setLevelFilter(v ?? "all")}>
              <SelectTrigger className="w-36 border-zinc-800 bg-zinc-900 text-zinc-300 h-9 text-sm">
                <SelectValue placeholder="Level" />
              </SelectTrigger>
              <SelectContent className="border-zinc-700 bg-zinc-900">
                {["all", "critical", "error", "warning"].map((l) => (
                  <SelectItem key={l} value={l} className="text-zinc-300 focus:bg-zinc-800">{l}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={workerFilter} onValueChange={(v) => setWorkerFilter(v ?? "all")}>
              <SelectTrigger className="w-52 border-zinc-800 bg-zinc-900 text-zinc-300 h-9 text-sm">
                <SelectValue placeholder="Worker" />
              </SelectTrigger>
              <SelectContent className="border-zinc-700 bg-zinc-900">
                <SelectItem value="all" className="text-zinc-300 focus:bg-zinc-800">All workers</SelectItem>
                {workers.map((w) => (
                  <SelectItem key={w} value={w} className="text-zinc-300 focus:bg-zinc-800 font-mono text-xs">{w}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="text-xs text-zinc-600">
            Showing {filtered.length} of {total} entries
          </div>

          {loading ? (
            <div className="space-y-2">
              {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-16 w-full bg-zinc-900" />)}
            </div>
          ) : (
            <ScrollArea className="h-[calc(100vh-320px)]">
              <div className="space-y-2 pr-2">
                {filtered.map((entry) => {
                  const cfg = levelConfig[entry.level];
                  const Icon = cfg.icon;
                  return (
                    <div key={entry.id} className={`rounded-lg border p-3.5 ${cfg.bg}`}>
                      <div className="flex items-start gap-3">
                        <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${cfg.color}`} />
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className={`inline-flex rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${cfg.badge}`}>
                              {entry.level}
                            </span>
                            <span className="font-mono text-[10px] text-zinc-500 bg-zinc-800 rounded px-1.5 py-0.5">
                              {entry.worker}
                            </span>
                            <span className="font-mono text-[10px] text-zinc-600">{entry.trace_id}</span>
                          </div>
                          <p className="mt-1.5 text-xs text-zinc-300 leading-relaxed">{entry.message}</p>
                          <div className="mt-2 flex items-center gap-4">
                            <span className="flex items-center gap-1 text-[10px] text-zinc-600">
                              <Clock className="h-3 w-3" />
                              {new Date(entry.timestamp).toLocaleString()}
                            </span>
                            <div className="flex items-center gap-1">
                              <span className="text-[10px] text-zinc-600">TTL</span>
                              {ttlBar(entry.ttl_remaining_s)}
                            </div>
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </ScrollArea>
          )}
        </div>
      </main>
    </div>
  );
}
