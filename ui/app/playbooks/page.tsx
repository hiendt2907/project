"use client";

import { useEffect, useState } from "react";
import { Sidebar } from "@/components/sidebar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Plus, Pencil, Trash2, Search, Bot, CheckCircle2, XCircle } from "lucide-react";

type PlaybookStep = string | { action_type?: string; target?: string; [k: string]: unknown };

type Playbook = {
  id: string;
  name: string;
  description?: string;
  siem_categories?: string[];
  severity_filter: string;
  actions?: string[];
  steps?: PlaybookStep[];
  auto_execute?: boolean;
  created_at?: string;
  updated_at?: string;
};

function stepLabel(s: PlaybookStep): string {
  if (typeof s === "string") return s;
  return s.action_type ?? JSON.stringify(s);
}

const CATEGORIES = ["k8s_threat", "pod_failure", "resource_pressure", "network_threat", "all"];
const SEVERITIES = ["critical", "warning", "info", "all"];

const severityColor: Record<string, string> = {
  critical: "bg-red-500/10 text-red-400 border-red-500/20",
  warning: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  info: "bg-blue-500/10 text-blue-400 border-blue-500/20",
};

const catColor: Record<string, string> = {
  k8s_threat: "bg-pink-500/10 text-pink-400 border-pink-500/20",
  pod_failure: "bg-orange-500/10 text-orange-400 border-orange-500/20",
  resource_pressure: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  network_threat: "bg-red-500/10 text-red-400 border-red-500/20",
};

function PlaybookForm({
  initial,
  onSave,
  onCancel,
}: {
  initial?: Partial<Playbook>;
  onSave: (data: Partial<Playbook>) => Promise<void>;
  onCancel: () => void;
}) {
  const [form, setForm] = useState({
    name: initial?.name ?? "",
    description: initial?.description ?? "",
    siem_categories: (initial?.siem_categories ?? []).join(", "),
    severity_filter: initial?.severity_filter ?? "critical",
    actions: (initial?.actions ?? []).join(", "),
    auto_execute: initial?.auto_execute ?? false,
  });
  const [saving, setSaving] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    await onSave({
      ...form,
      siem_categories: form.siem_categories.split(",").map((s) => s.trim()).filter(Boolean),
      actions: form.actions.split(",").map((s) => s.trim()).filter(Boolean),
    });
    setSaving(false);
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="space-y-1.5">
        <Label className="text-xs uppercase tracking-widest text-zinc-500">Name</Label>
        <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required
          className="border-zinc-700 bg-zinc-800 text-zinc-100" placeholder="Pod CrashLoop Remediation" />
      </div>
      <div className="space-y-1.5">
        <Label className="text-xs uppercase tracking-widest text-zinc-500">Description</Label>
        <Textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })}
          className="border-zinc-700 bg-zinc-800 text-zinc-100 resize-none" rows={2} />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-1.5">
          <Label className="text-xs uppercase tracking-widest text-zinc-500">SIEM Categories (comma-separated)</Label>
          <Input value={form.siem_categories} onChange={(e) => setForm({ ...form, siem_categories: e.target.value })}
            className="border-zinc-700 bg-zinc-800 text-zinc-100" placeholder="k8s_threat, pod_failure" />
        </div>
        <div className="space-y-1.5">
          <Label className="text-xs uppercase tracking-widest text-zinc-500">Severity</Label>
          <Select value={form.severity_filter} onValueChange={(v) => setForm({ ...form, severity_filter: v ?? "critical" })}>
            <SelectTrigger className="border-zinc-700 bg-zinc-800 text-zinc-100">
              <SelectValue />
            </SelectTrigger>
            <SelectContent className="border-zinc-700 bg-zinc-900">
              {["critical", "warning", "info"].map((s) => (
                <SelectItem key={s} value={s} className="text-zinc-300 focus:bg-zinc-800">{s}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      <div className="space-y-1.5">
        <Label className="text-xs uppercase tracking-widest text-zinc-500">Actions (comma-separated)</Label>
        <Input value={form.actions} onChange={(e) => setForm({ ...form, actions: e.target.value })}
          className="border-zinc-700 bg-zinc-800 text-zinc-100" placeholder="collect_pod_logs, rollout_restart_deployment" />
      </div>
      <div className="flex items-center gap-3">
        <input type="checkbox" id="auto_execute" checked={form.auto_execute}
          onChange={(e) => setForm({ ...form, auto_execute: e.target.checked })}
          className="h-4 w-4 rounded border-zinc-600 bg-zinc-800 accent-cyan-500" />
        <Label htmlFor="auto_execute" className="text-sm text-zinc-300 cursor-pointer">Auto-execute (no HITL required)</Label>
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <Button type="button" variant="ghost" onClick={onCancel} className="text-zinc-400 hover:text-zinc-100">Cancel</Button>
        <Button type="submit" disabled={saving} className="bg-cyan-500 text-zinc-950 font-semibold hover:bg-cyan-400">
          {saving ? "Saving…" : "Save Playbook"}
        </Button>
      </div>
    </form>
  );
}

export default function PlaybooksPage() {
  const [playbooks, setPlaybooks] = useState<Playbook[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [catFilter, setCatFilter] = useState("all");
  const [sevFilter, setSevFilter] = useState("all");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<Playbook | null>(null);

  async function fetchPlaybooks() {
    const params = new URLSearchParams();
    if (catFilter !== "all") params.set("category", catFilter);
    if (sevFilter !== "all") params.set("severity", sevFilter);
    const res = await fetch(`/api/playbooks?${params}`);
    const data = await res.json();
    setPlaybooks(data);
    setLoading(false);
  }

  useEffect(() => { fetchPlaybooks(); }, [catFilter, sevFilter]);

  async function handleSave(data: Partial<Playbook>) {
    if (editing) {
      await fetch(`/api/playbooks/${editing.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
      setPlaybooks((prev) => prev.map((p) => p.id === editing.id ? { ...p, ...data } as Playbook : p));
    } else {
      const res = await fetch("/api/playbooks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
      const created = await res.json();
      setPlaybooks((prev) => [created, ...prev]);
    }
    setDialogOpen(false);
    setEditing(null);
  }

  async function handleDelete(id: string) {
    await fetch(`/api/playbooks/${id}`, { method: "DELETE" });
    setPlaybooks((prev) => prev.filter((p) => p.id !== id));
  }

  const filtered = playbooks.filter((p) => {
    if (search === "") return true;
    const q = search.toLowerCase();
    return (p.name ?? "").toLowerCase().includes(q) || (p.description ?? "").toLowerCase().includes(q);
  });

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        <header className="sticky top-0 z-10 flex h-16 items-center justify-between border-b border-zinc-800 bg-zinc-950/80 px-6 backdrop-blur">
          <div>
            <h1 className="text-base font-semibold text-zinc-100">Playbook Manager</h1>
            <p className="text-xs text-zinc-500">Automated response playbooks · Redis JSON backend</p>
          </div>
          <Button
            onClick={() => { setEditing(null); setDialogOpen(true); }}
            className="bg-cyan-500 text-zinc-950 font-semibold hover:bg-cyan-400 h-8 text-xs"
          >
            <Plus className="mr-1.5 h-3.5 w-3.5" />
            New Playbook
          </Button>
        </header>

        <div className="p-6 space-y-4">
          <div className="flex flex-wrap gap-3">
            <div className="relative flex-1 min-w-[200px]">
              <Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search playbooks…"
                className="pl-9 border-zinc-800 bg-zinc-900 text-zinc-100 h-9 text-sm"
              />
            </div>
            <Select value={catFilter} onValueChange={(v) => setCatFilter(v ?? "all")}>
              <SelectTrigger className="w-44 border-zinc-800 bg-zinc-900 text-zinc-300 h-9 text-sm">
                <SelectValue placeholder="Category" />
              </SelectTrigger>
              <SelectContent className="border-zinc-700 bg-zinc-900">
                {CATEGORIES.map((c) => (
                  <SelectItem key={c} value={c} className="text-zinc-300 focus:bg-zinc-800">{c}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={sevFilter} onValueChange={(v) => setSevFilter(v ?? "all")}>
              <SelectTrigger className="w-36 border-zinc-800 bg-zinc-900 text-zinc-300 h-9 text-sm">
                <SelectValue placeholder="Severity" />
              </SelectTrigger>
              <SelectContent className="border-zinc-700 bg-zinc-900">
                {SEVERITIES.map((s) => (
                  <SelectItem key={s} value={s} className="text-zinc-300 focus:bg-zinc-800">{s}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {loading ? (
            <div className="space-y-3">
              {Array.from({ length: 4 }).map((_, i) => (
                <Card key={i} className="border-zinc-800 bg-zinc-900/60">
                  <CardContent className="p-4"><Skeleton className="h-20 w-full bg-zinc-800" /></CardContent>
                </Card>
              ))}
            </div>
          ) : filtered.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-20 text-zinc-600">
              <Bot className="h-10 w-10 mb-3 opacity-30" />
              <p className="text-sm">No playbooks match your filter</p>
            </div>
          ) : (
            <div className="space-y-3">
              {filtered.map((pb) => (
                <Card key={pb.id} className="border-zinc-800 bg-zinc-900/60 hover:border-zinc-700 transition-colors">
                  <CardContent className="p-5">
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <h3 className="text-sm font-semibold text-zinc-100">{pb.name}</h3>
                          <span className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${severityColor[pb.severity_filter] ?? "bg-zinc-800 text-zinc-400 border-zinc-700"}`}>
                            {pb.severity_filter}
                          </span>
                          {pb.auto_execute ? (
                            <span className="flex items-center gap-1 text-[10px] text-emerald-400">
                              <CheckCircle2 className="h-3 w-3" /> auto-execute
                            </span>
                          ) : (
                            <span className="flex items-center gap-1 text-[10px] text-amber-400">
                              <XCircle className="h-3 w-3" /> HITL required
                            </span>
                          )}
                        </div>
                        <p className="mt-1 text-xs text-zinc-500">{pb.description}</p>
                        <div className="mt-2.5 flex flex-wrap gap-1.5">
                          {(pb.siem_categories ?? []).map((c) => (
                            <span key={c} className={`inline-flex rounded border px-1.5 py-0.5 text-[10px] font-medium ${catColor[c] ?? "bg-zinc-800 text-zinc-500 border-zinc-700"}`}>
                              {c}
                            </span>
                          ))}
                        </div>
                        <div className="mt-2 flex flex-wrap gap-1">
                          {(pb.actions ?? (pb.steps ?? []).map(stepLabel)).map((a, i) => (
                            <span key={`${a}-${i}`} className="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400">
                              {a}
                            </span>
                          ))}
                        </div>
                      </div>
                      <div className="flex shrink-0 gap-1">
                        <Button
                          size="icon"
                          variant="ghost"
                          className="h-7 w-7 text-zinc-500 hover:text-cyan-400 hover:bg-zinc-800"
                          onClick={() => { setEditing(pb); setDialogOpen(true); }}
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          size="icon"
                          variant="ghost"
                          className="h-7 w-7 text-zinc-500 hover:text-red-400 hover:bg-zinc-800"
                          onClick={() => handleDelete(pb.id)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </div>

        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogContent className="max-w-2xl border-zinc-800 bg-zinc-900 text-zinc-100">
            <DialogHeader>
              <DialogTitle className="text-zinc-100">{editing ? "Edit Playbook" : "New Playbook"}</DialogTitle>
            </DialogHeader>
            <PlaybookForm
              initial={editing ?? undefined}
              onSave={handleSave}
              onCancel={() => { setDialogOpen(false); setEditing(null); }}
            />
          </DialogContent>
        </Dialog>
      </main>
    </div>
  );
}
