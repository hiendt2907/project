"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Sidebar } from "@/components/sidebar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  CheckCircle2,
  XCircle,
  RefreshCw,
  Copy,
  Check,
  ArrowRight,
  KeyRound,
  Loader2,
} from "lucide-react";

interface ChecklistItem {
  key: string;
  label: string;
  status: boolean;
  detail?: string;
}

interface ChecklistData {
  checklist: ChecklistItem[];
}

const QUICK_START = `# 1. Test alert ingestion
curl -X POST http://localhost:8000/webhook/prometheus \\
  -H "Content-Type: application/json" \\
  -d '{"alerts":[{"alertname":"TestCPU","labels":{"severity":"warning","namespace":"multi-agent"},"status":"firing","startsAt":"","endsAt":"","generatorURL":""}]}'

# 2. Check worker health
curl http://localhost:8090/healthz

# 3. View audit chain
curl -H "Authorization: Bearer $OMNI_API_KEY" \\
  http://localhost:8000/crat/stats`;

function CopyBox({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);

  function copy() {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <div className="relative rounded-lg border border-zinc-800 bg-zinc-950">
      <pre className="overflow-x-auto p-4 text-[11px] leading-relaxed text-zinc-300 font-mono">{code}</pre>
      <button
        onClick={copy}
        className="absolute right-2 top-2 flex items-center gap-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-[10px] text-zinc-400 hover:border-zinc-600 hover:text-zinc-200 transition-colors"
      >
        {copied ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />}
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

export default function OnboardingPage() {
  const [data, setData] = useState<ChecklistData | null>(null);
  const [loading, setLoading] = useState(true);
  const [apiKeyCopied, setApiKeyCopied] = useState(false);

  const apiKey = typeof window !== "undefined"
    ? (process.env.NEXT_PUBLIC_API_KEY ?? "")
    : "";

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/onboarding", { cache: "no-store" });
      if (res.ok) setData(await res.json());
    } catch {
      // keep existing
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const checklist = data?.checklist ?? [];
  const allDone = checklist.length > 0 && checklist.every((c) => c.status);

  function copyApiKey() {
    if (!apiKey) return;
    navigator.clipboard.writeText(apiKey).then(() => {
      setApiKeyCopied(true);
      setTimeout(() => setApiKeyCopied(false), 2000);
    });
  }

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        <header className="sticky top-0 z-10 flex h-16 items-center justify-between border-b border-zinc-800 bg-zinc-950/80 px-6 backdrop-blur">
          <div>
            <h1 className="text-base font-semibold text-zinc-100">Omni Setup</h1>
            <p className="text-xs text-zinc-500">Complete the checklist to start receiving alerts</p>
          </div>
          <button
            onClick={load}
            className="flex items-center gap-1.5 rounded border border-zinc-700 px-2.5 py-1.5 text-xs text-zinc-400 hover:border-zinc-600 hover:text-zinc-100 transition-colors"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Recheck
          </button>
        </header>

        <div className="mx-auto max-w-2xl p-6 space-y-6">
          {/* Checklist */}
          <Card className="border-zinc-800 bg-zinc-900/60">
            <CardHeader className="p-4 pb-2">
              <CardTitle className="text-sm text-zinc-100">Setup Checklist</CardTitle>
            </CardHeader>
            <CardContent className="p-4 pt-0 space-y-2">
              {loading ? (
                Array.from({ length: 6 }).map((_, i) => (
                  <Skeleton key={i} className="h-12 bg-zinc-800" />
                ))
              ) : (
                checklist.map((item) => (
                  <div
                    key={item.key}
                    className={`flex items-start gap-3 rounded-lg border px-3 py-3 ${
                      item.status
                        ? "border-emerald-900/40 bg-emerald-500/5"
                        : "border-zinc-800 bg-zinc-900/40"
                    }`}
                  >
                    {item.status ? (
                      <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
                    ) : (
                      <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-zinc-600" />
                    )}
                    <div className="flex-1 min-w-0">
                      <p className={`text-sm ${item.status ? "text-zinc-200" : "text-zinc-400"}`}>
                        {item.label}
                      </p>
                      {item.detail && (
                        <p className="mt-0.5 text-[11px] text-zinc-600">{item.detail}</p>
                      )}
                    </div>
                  </div>
                ))
              )}
            </CardContent>
          </Card>

          {/* API Key section */}
          <Card className="border-zinc-800 bg-zinc-900/60">
            <CardHeader className="p-4 pb-2">
              <CardTitle className="flex items-center gap-2 text-sm text-zinc-100">
                <KeyRound className="h-4 w-4 text-cyan-400" />
                API Key
              </CardTitle>
            </CardHeader>
            <CardContent className="p-4 pt-0 space-y-3">
              <p className="text-xs text-zinc-500">
                Use this key as the Bearer token when calling gateway endpoints.
              </p>
              <div className="flex items-center gap-2">
                <div className="flex-1 rounded border border-zinc-700 bg-zinc-950 px-3 py-2 font-mono text-sm text-zinc-300">
                  {apiKey ? `${apiKey.slice(0, 8)}${"•".repeat(Math.max(0, apiKey.length - 8))}` : "Configure NEXT_PUBLIC_API_KEY in .env"}
                </div>
                <button
                  disabled={!apiKey}
                  onClick={copyApiKey}
                  className="flex items-center gap-1.5 rounded border border-zinc-700 px-3 py-2 text-xs text-zinc-400 hover:border-zinc-500 hover:text-zinc-100 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  {apiKeyCopied ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
                  {apiKeyCopied ? "Copied" : "Copy"}
                </button>
              </div>
            </CardContent>
          </Card>

          {/* Quick start commands */}
          <Card className="border-zinc-800 bg-zinc-900/60">
            <CardHeader className="p-4 pb-2">
              <CardTitle className="text-sm text-zinc-100">Quick Start Commands</CardTitle>
            </CardHeader>
            <CardContent className="p-4 pt-0">
              <CopyBox code={QUICK_START} />
            </CardContent>
          </Card>

          {/* CTA */}
          {allDone ? (
            <Link
              href="/"
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-cyan-600 py-3 text-sm font-medium text-white hover:bg-cyan-500 transition-colors"
            >
              Go to Dashboard
              <ArrowRight className="h-4 w-4" />
            </Link>
          ) : (
            <div className="flex items-center justify-center gap-2 rounded-lg border border-zinc-800 py-3 text-sm text-zinc-500">
              <Loader2 className="h-4 w-4 animate-spin" />
              Complete all checklist items to continue
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
