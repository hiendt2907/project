"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";
import {
  LayoutDashboard,
  BookOpen,
  AlertTriangle,
  Activity,
  LogOut,
  Bot,
  BarChart3,
} from "lucide-react";
import { signOut } from "next-auth/react";

const nav = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/playbooks", label: "Playbooks", icon: BookOpen },
  { href: "/ledger", label: "Error Ledger", icon: AlertTriangle },
  { href: "/kpi", label: "KPI Dashboard", icon: BarChart3 },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="flex h-screen w-60 flex-col border-r border-zinc-800 bg-zinc-950">
      <div className="flex h-16 items-center gap-3 border-b border-zinc-800 px-5">
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-cyan-500/10 ring-1 ring-cyan-500/30">
          <Bot className="h-4 w-4 text-cyan-400" />
        </div>
        <div>
          <p className="text-sm font-semibold tracking-tight text-zinc-100">Omni SRE</p>
          <p className="text-[10px] text-zinc-500 uppercase tracking-widest">Control Plane</p>
        </div>
      </div>

      <nav className="flex-1 space-y-0.5 px-3 py-4">
        {nav.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className={cn(
              "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
              pathname === href
                ? "bg-cyan-500/10 text-cyan-400 ring-1 ring-inset ring-cyan-500/20"
                : "text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-100"
            )}
          >
            <Icon className="h-4 w-4 shrink-0" />
            {label}
          </Link>
        ))}
      </nav>

      <div className="border-t border-zinc-800 px-3 py-4 space-y-1">
        <div className="flex items-center gap-3 rounded-md px-3 py-2">
          <Activity className="h-4 w-4 text-emerald-400" />
          <div>
            <p className="text-xs font-medium text-zinc-300">System Live</p>
            <p className="text-[10px] text-zinc-600">multi-agent ns</p>
          </div>
        </div>
        <button
          onClick={() => signOut({ callbackUrl: "/login" })}
          className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm text-zinc-500 hover:bg-zinc-800/60 hover:text-red-400 transition-colors"
        >
          <LogOut className="h-4 w-4" />
          Sign out
        </button>
      </div>
    </aside>
  );
}
