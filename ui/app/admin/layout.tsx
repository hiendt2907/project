"use client";

import { Suspense, useEffect, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { Sidebar } from "@/components/sidebar";
import { TenantSelector } from "@/components/tenant-selector";
import { Radio } from "lucide-react";

// Page titles per admin route — drives the sticky header label.
const TITLES: Record<string, string> = {
  "/admin": "Overview",
  "/admin/tier": "Autonomy Tier",
  "/admin/risk-class": "Risk Classes",
  "/admin/flags": "Runtime Flags",
  "/admin/tenants": "Tenants & API Keys",
  "/admin/hitl": "HITL Queue",
  "/admin/guide": "User Guide",
};

function HeaderClock() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(t);
  }, []);
  return <span className="text-zinc-600">{new Date(now).toLocaleTimeString()}</span>;
}

export default function AdminLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const title = TITLES[pathname] ?? "Admin";

  return (
    <div className="flex h-full">
      <Sidebar />
      <main className="flex-1 overflow-auto bg-zinc-950 font-mono text-[11px]">
        <div className="sticky top-0 z-10 flex items-center justify-between px-4 h-9 border-b border-zinc-800 bg-zinc-950">
          <div className="flex items-center gap-3 text-[10px]">
            <span className="text-amber-400 font-semibold tracking-widest uppercase">Admin</span>
            <span className="text-zinc-700">/</span>
            <span className="text-zinc-300 font-semibold tracking-wide">{title}</span>
            <span className="text-zinc-700">ns:multi-agent</span>
          </div>
          <div className="flex items-center gap-3 text-[10px]">
            <Suspense fallback={null}>
              <TenantSelector />
            </Suspense>
            <HeaderClock />
            <span className="flex items-center gap-1 text-emerald-400">
              <Radio size={9} className="animate-pulse" />
              live
            </span>
          </div>
        </div>
        {children}
      </main>
    </div>
  );
}
