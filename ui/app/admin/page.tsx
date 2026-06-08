"use client";

import { Suspense } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { StatCard } from "@/components/shared/primitives";
import { pct } from "@/components/shared/fmt";
import { useAdminData } from "@/components/admin/useAdminData";
import {
  Gauge,
  ShieldAlert,
  ToggleRight,
  Building2,
  UserCheck,
  BookOpen,
  ArrowRight,
} from "lucide-react";

interface NavCard {
  href: string;
  label: string;
  desc: string;
  icon: typeof Gauge;
  accent: string;
}

interface CardGroup {
  title: string;
  hint: string;
  cards: NavCard[];
}

// Grouped admin navigation — Autonomy (decision authority) vs Configuration (infra)
// vs Help. Observability moved out (belongs to the Ops console, omni.ai-agent.local).
const GROUPS: CardGroup[] = [
  {
    title: "Autonomy",
    hint: "ai được phép tự chạy tới đâu · source-of-truth omni_admin",
    cards: [
      { href: "/admin/tier", label: "Autonomy Tier", desc: "shadow · assist · auto — nâng tier cần 2-step confirm, fail-closed", icon: Gauge, accent: "text-amber-400 ring-amber-500/30" },
      { href: "/admin/risk-class", label: "Risk Classes", desc: "gán READONLY → HIGH cho từng tool · dangerous tools khoá HIGH", icon: ShieldAlert, accent: "text-rose-400 ring-rose-500/30" },
      { href: "/admin/hitl", label: "HITL Queue", desc: "duyệt thủ công action chờ người · publish quyết định qua Kafka", icon: UserCheck, accent: "text-emerald-400 ring-emerald-500/30" },
    ],
  },
  {
    title: "Configuration",
    hint: "cấu hình hệ thống · tenant & feature flags",
    cards: [
      { href: "/admin/flags", label: "Runtime Flags", desc: "feature flags key-value · persist vào omni_admin.runtime_flag", icon: ToggleRight, accent: "text-cyan-400 ring-cyan-500/30" },
      { href: "/admin/tenants", label: "Tenants & API Keys", desc: "vòng đời tenant · cấp / thu hồi key (lưu hash-only)", icon: Building2, accent: "text-violet-400 ring-violet-500/30" },
    ],
  },
  {
    title: "Help",
    hint: "hướng dẫn dùng từng panel",
    cards: [
      { href: "/admin/guide", label: "User Guide", desc: "giải thích Tier · Risk Class · Flags · Tenants · HITL + cách thao tác", icon: BookOpen, accent: "text-sky-400 ring-sky-500/30" },
    ],
  },
];

function NavCardLink({ c }: { c: NavCard }) {
  return (
    <Link
      href={c.href}
      className="group flex flex-col gap-3 rounded-lg border border-zinc-800 bg-zinc-900/40 p-4 transition-colors hover:border-zinc-700 hover:bg-zinc-900"
    >
      <div className="flex items-center justify-between">
        <span className={`flex h-9 w-9 items-center justify-center rounded-md bg-zinc-950 ring-1 ${c.accent}`}>
          <c.icon className="h-4 w-4" />
        </span>
        <ArrowRight className="h-3.5 w-3.5 text-zinc-700 transition-transform group-hover:translate-x-0.5 group-hover:text-zinc-400" />
      </div>
      <div>
        <p className="text-[13px] font-semibold text-zinc-100">{c.label}</p>
        <p className="mt-1 text-[10px] leading-relaxed text-zinc-500">{c.desc}</p>
      </div>
    </Link>
  );
}

function Inner() {
  const tenant = useSearchParams().get("tenant") ?? "default";
  const { pods, crat, kpi, siem } = useAdminData(tenant);

  const healthy = (pods ?? []).filter((p) => p.status === "healthy").length;
  const degraded = (pods ?? []).filter((p) => p.status === "degraded").length;
  const down = (pods ?? []).filter((p) => p.status === "unhealthy").length;
  const maxKafkaLag = siem ? Math.max(0, ...siem.pipeline.kafka_lag.map((k) => k.lag)) : 0;

  return (
    <>
      <div className="flex gap-px border-b border-zinc-800 bg-zinc-950">
        <StatCard label="workers" value={pods ? `${healthy}/${pods.length}` : "—"} color={down > 0 ? "text-rose-400" : degraded > 0 ? "text-amber-400" : "text-emerald-400"} sub="healthy" />
        <StatCard label="kafka lag" value={siem ? `${maxKafkaLag}` : "—"} color={maxKafkaLag >= 1000 ? "text-rose-400" : maxKafkaLag >= 100 ? "text-amber-400" : "text-emerald-400"} sub="max msgs" />
        <StatCard label="kpi acceptance" value={pct(kpi?.acceptance_rate ?? null)} color={kpi?.acceptance_rate != null ? (kpi.acceptance_rate >= 0.8 ? "text-emerald-400" : kpi.acceptance_rate >= 0.6 ? "text-amber-400" : "text-rose-400") : "text-zinc-600"} sub={`${kpi?.accepted ?? 0}/${kpi?.total ?? 0} adv`} />
        <StatCard label="crat blocks" value={crat ? `${crat.total ?? 0}` : "—"} color={crat ? "text-sky-400" : "text-zinc-600"} sub="24h" />
      </div>

      <div className="p-4 space-y-7">
        {GROUPS.map((g) => (
          <section key={g.title} className="space-y-3">
            <div className="flex items-center gap-2">
              <span className="text-amber-400 text-[10px] font-semibold tracking-[0.2em] uppercase">{g.title}</span>
              <span className="h-px flex-1 bg-gradient-to-r from-amber-500/30 to-transparent" />
              <span className="text-[9px] text-zinc-600">{g.hint}</span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              {g.cards.map((c) => (
                <NavCardLink key={c.href} c={c} />
              ))}
            </div>
          </section>
        ))}

        <p className="text-[10px] text-zinc-600 border-t border-zinc-800/60 pt-3">
          Telemetry, Workers, KPI, CRAT, Deploy &amp; Remote Agents đã chuyển sang{" "}
          <span className="text-zinc-400">Ops Console</span> (omni.ai-agent.local) — trang Admin này chỉ phụ trách cấu hình.
        </p>
      </div>
    </>
  );
}

export default function AdminPage() {
  return (
    <Suspense>
      <Inner />
    </Suspense>
  );
}
