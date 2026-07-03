"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";
import {
  LayoutDashboard,
  BookOpen,
  AlertTriangle,
  Activity,
  LogOut,
  Bot,
  BarChart3,
  Wifi,
  WifiOff,
  AlertCircle,
  Bell,
  Server,
  Shield,
  Rocket,
  CircleHelp,
  ExternalLink,
  MonitorCheck,
  Gauge,
  ShieldAlert,
  ToggleRight,
  Building2,
  UserCheck,
  FlaskConical,
  Workflow,
  Brain,
} from "lucide-react";
import { signOut } from "next-auth/react";
import { useOmniUiRealm } from "@/components/providers";
import { PORTAL_HOST } from "@/lib/omni-ui-realm";

type NavItem =
  | { href: string; label: string; icon: typeof LayoutDashboard; badge?: boolean }
  | { section: string }
  | { external: string; label: string; icon: typeof ExternalLink };

const navOps: NavItem[] = [
  { href: "/operator", label: "Operator Console", icon: LayoutDashboard },
  { href: "/understanding", label: "Understanding", icon: Brain },
  { href: "/incidents", label: "Incidents", icon: Bell, badge: true },
  { href: "/siem", label: "SIEM", icon: Shield },
  { href: "/playbooks", label: "Playbooks", icon: BookOpen },
  { href: "/kpi", label: "KPI Dashboard", icon: BarChart3 },
  { href: "/ledger", label: "Error Ledger", icon: AlertTriangle },
  { href: "/remote-agents", label: "Remote Agents", icon: MonitorCheck },
  { section: "Admin" },
  { external: `//${PORTAL_HOST}/admin`, label: "Admin Console", icon: ExternalLink },
];

const navPortal: NavItem[] = [
  { href: "/admin", label: "Overview", icon: LayoutDashboard },
  { section: "Autonomy" },
  { href: "/admin/tier", label: "Autonomy Tier", icon: Gauge },
  { href: "/admin/risk-class", label: "Risk Classes", icon: ShieldAlert },
  { href: "/admin/hitl", label: "HITL Queue", icon: UserCheck, badge: true },
  { section: "Configuration" },
  { href: "/admin/flags", label: "Runtime Flags", icon: ToggleRight },
  { href: "/admin/tenants", label: "Tenants & Keys", icon: Building2 },
  { href: "/remote-agents", label: "Remote Agents", icon: MonitorCheck },
  { section: "Diagnostics" },
  { href: "/pipeline", label: "Pipeline", icon: Workflow },
  { href: "/simulator", label: "Simulator", icon: FlaskConical },
  { href: "/understanding", label: "Understanding", icon: Brain },
  { href: "/admin/kb", label: "RAG Knowledge", icon: Brain },
  { section: "Help" },
  { href: "/admin/guide", label: "User Guide", icon: BookOpen },
  { href: "/onboarding", label: "Setup", icon: CircleHelp },
];

const navFull: NavItem[] = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/understanding", label: "Understanding", icon: Brain },
  { href: "/playbooks", label: "Playbooks", icon: BookOpen },
  { href: "/ledger", label: "Error Ledger", icon: AlertTriangle },
  { href: "/kpi", label: "KPI Dashboard", icon: BarChart3 },
  { href: "/incidents", label: "Incidents", icon: Bell, badge: true },
  { href: "/workers", label: "Workers", icon: Server },
  { href: "/remote-agents", label: "Remote Agents", icon: MonitorCheck },
  { href: "/siem", label: "SIEM", icon: Shield },
  { href: "/deploy", label: "Deploy", icon: Rocket },
  { href: "/pipeline", label: "Pipeline", icon: Workflow },
  { href: "/simulator", label: "Simulator", icon: FlaskConical },
  { section: "Config" },
  { href: "/onboarding", label: "Setup", icon: CircleHelp },
];

function useHitlCount(enabled: boolean): number {
  const [count, setCount] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;

    async function check() {
      try {
        const res = await fetch("/api/incidents", { cache: "no-store" });
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (!cancelled) setCount(data.hitl_pending_count ?? 0);
      } catch {
        // ignore
      }
    }

    check();
    const id = setInterval(check, 15_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [enabled]);

  return count;
}

type SystemStatus = "online" | "degraded" | "offline" | "loading";

function useSystemStatus(): SystemStatus {
  const [status, setStatus] = useState<SystemStatus>("loading");

  useEffect(() => {
    let cancelled = false;

    async function check() {
      try {
        const res = await fetch("/api/agents", { cache: "no-store" });
        if (!res.ok) {
          if (!cancelled) setStatus("offline");
          return;
        }
        const data = await res.json();
        if (cancelled) return;
        const overall: string = data.overall ?? "unknown";
        if (overall === "ok") setStatus("online");
        else if (overall === "degraded") setStatus("degraded");
        else if (overall === "unhealthy") setStatus("offline");
        else setStatus("offline");
      } catch {
        if (!cancelled) setStatus("offline");
      }
    }

    check();
    const id = setInterval(check, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return status;
}

const statusConfig: Record<
  SystemStatus,
  { label: string; sub: string; icon: typeof Wifi; color: string }
> = {
  online: {
    label: "System Online",
    sub: "all workers healthy",
    icon: Wifi,
    color: "text-emerald-400",
  },
  degraded: {
    label: "Degraded",
    sub: "some checks failing",
    icon: AlertCircle,
    color: "text-amber-400",
  },
  offline: {
    label: "System Offline",
    sub: "workers unreachable",
    icon: WifiOff,
    color: "text-red-400",
  },
  loading: {
    label: "Checking…",
    sub: "multi-agent ns",
    icon: Activity,
    color: "text-zinc-500",
  },
};

export function Sidebar() {
  const pathname = usePathname();
  const realm = useOmniUiRealm();
  const nav =
    realm === "portal" ? navPortal : realm === "ops" ? navOps : navFull;
  const hitlCount = useHitlCount(realm === "ops" || realm === "local");
  const systemStatus = useSystemStatus();
  const cfg = statusConfig[systemStatus];
  const subtitle =
    realm === "portal" ? "Admin Console"
    : realm === "ops" ? "Operator Console"
    : "Control Plane";

  return (
    <aside className="flex h-screen w-60 flex-col border-r border-zinc-800 bg-zinc-950">
      <div className="flex h-16 items-center gap-3 border-b border-zinc-800 px-5">
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-cyan-500/10 ring-1 ring-cyan-500/30">
          <Bot className="h-4 w-4 text-cyan-400" />
        </div>
        <div>
          <p className="text-sm font-semibold tracking-tight text-zinc-100">Omni SRE</p>
          <p className="text-[10px] text-zinc-500 uppercase tracking-widest">{subtitle}</p>
        </div>
      </div>

      <nav className="flex-1 space-y-0.5 overflow-y-auto px-3 py-4">
        {nav.map((item, idx) => {
          if ("section" in item) {
            return (
              <div key={`section-${idx}`} className="mb-1 mt-3 px-3">
                <p className="text-[9px] font-semibold uppercase tracking-[0.12em] text-zinc-600">
                  {item.section}
                </p>
              </div>
            );
          }
          if ("external" in item) {
            const { external, label, icon: Icon } = item;
            return (
              <a
                key={external}
                href={external}
                className="flex items-center gap-3 rounded-md px-3 py-2 text-sm text-zinc-400 transition-colors hover:bg-zinc-800/60 hover:text-cyan-400"
              >
                <Icon className="h-4 w-4 shrink-0" />
                <span className="flex-1">{label}</span>
              </a>
            );
          }
          const { href, label, icon: Icon, badge } = item;
          const showBadge = badge && hitlCount > 0;
          return (
            <Link
              key={href}
              href={href}
              className={cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
                pathname === href
                  ? "bg-cyan-500/10 text-cyan-400 ring-1 ring-inset ring-cyan-500/20"
                  : "text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-100",
              )}
            >
              <Icon className="h-4 w-4 shrink-0" />
              <span className="flex-1">{label}</span>
              {showBadge && (
                <span className="ml-auto flex h-4 min-w-4 items-center justify-center rounded-full bg-rose-500 px-1 font-mono text-[9px] font-bold text-white">
                  {hitlCount}
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      <div className="space-y-1 border-t border-zinc-800 px-3 py-4">
        <div className="flex items-center gap-3 rounded-md px-3 py-2">
          <cfg.icon className={`h-4 w-4 shrink-0 ${cfg.color}`} />
          <div>
            <p className={`text-xs font-medium ${cfg.color}`}>{cfg.label}</p>
            <p className="text-[10px] text-zinc-600">{cfg.sub}</p>
          </div>
        </div>
        <button
          onClick={() => signOut({ callbackUrl: "/login" })}
          className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm text-zinc-500 transition-colors hover:bg-zinc-800/60 hover:text-red-400"
        >
          <LogOut className="h-4 w-4" />
          Sign out
        </button>
      </div>
    </aside>
  );
}
