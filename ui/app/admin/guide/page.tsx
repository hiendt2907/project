"use client";

import Link from "next/link";
import {
  Gauge,
  ShieldAlert,
  ToggleRight,
  Building2,
  UserCheck,
  ArrowRight,
  Database,
  ShieldCheck,
} from "lucide-react";

// Read-only user guide for the Admin Console. Explains each configuration surface,
// how it persists, and the exact steps an operator takes. No backend calls.

interface GuideStep {
  text: string;
}

interface GuideEntry {
  href: string;
  label: string;
  icon: typeof Gauge;
  accent: string;
  what: string;
  store: string;
  steps: GuideStep[];
  note?: string;
}

const ENTRIES: GuideEntry[] = [
  {
    href: "/admin/tier",
    label: "Autonomy Tier",
    icon: Gauge,
    accent: "text-amber-400 ring-amber-500/30",
    what:
      "Quyết định mức tự chủ của toàn hệ thống cho một tenant: shadow (chỉ quan sát) → assist (gợi ý + chờ duyệt) → auto (tự chạy trong giới hạn risk-class).",
    store: "omni_admin.autonomy_tier_state (+ autonomy_tier_history)",
    steps: [
      { text: "Chọn tenant ở thanh trên cùng (mặc định: default)." },
      { text: "Xem Readiness: Wilson lower-bound, tỉ lệ chấp nhận, số ngày chạy — điều kiện để được nâng tier." },
      { text: "Bấm tier muốn đặt. HẠ tier có hiệu lực ngay (an toàn hơn)." },
      { text: "NÂNG tier (vd shadow→assist) yêu cầu xác nhận 2 bước — nếu readiness chưa đạt phải tick 'forced'." },
    ],
    note: "Operator-only, fail-closed. Worker không bao giờ tự nhảy tier.",
  },
  {
    href: "/admin/risk-class",
    label: "Risk Classes",
    icon: ShieldAlert,
    accent: "text-rose-400 ring-rose-500/30",
    what:
      "Gán mức rủi ro cho từng tool (kubectl/exec/delete…). Risk-class là taxonomy CỐ ĐỊNH gồm 4 mức: READONLY · LOW · MEDIUM · HIGH. Bạn không 'tạo mức mới' mà override mức của một tool.",
    store: "omni_admin.risk_class_override (ghép bảng tĩnh STATIC_RISK_CLASS)",
    steps: [
      { text: "Mở 'Assign / Override' phía trên bảng — chọn tool + mức risk + lý do, rồi Apply." },
      { text: "Hoặc bấm thẳng ô R/L/M/H trên hàng tool tương ứng để override nhanh." },
      { text: "HẠ rủi ro (mức < mặc định tĩnh) = tăng quyền tự chạy → cần xác nhận 2 bước." },
      { text: "Tool gắn 🔒 là dangerous (vd delete-namespace) — khoá cứng HIGH, không hạ được." },
    ],
    note: "Tier auto chỉ tự chạy tool ≤ ngưỡng risk cho phép; phần còn lại rớt xuống HITL.",
  },
  {
    href: "/admin/hitl",
    label: "HITL Queue",
    icon: UserCheck,
    accent: "text-emerald-400 ring-emerald-500/30",
    what:
      "Hàng đợi action chờ con người duyệt (Human-In-The-Loop). Song song với kênh Telegram — duyệt ở đâu cũng được, ghi cùng một ledger.",
    store: "omni_admin.hitl_decision (+ crat_outbox + Kafka omni-hitl-decisions)",
    steps: [
      { text: "Mỗi dòng = 1 action đang chờ, kèm tool_name, risk_class, tier tại thời điểm sinh." },
      { text: "APPROVED → worker định tuyến sang omni-actions để executor chạy." },
      { text: "REJECTED → đẩy omni-action-feedback để analyst học lại." },
      { text: "Một pending chỉ quyết định MỘT lần — không ghi đè." },
    ],
    note: "CRAT-intent durable trong outbox TRƯỚC khi publish Kafka (fail-closed).",
  },
  {
    href: "/admin/flags",
    label: "Runtime Flags",
    icon: ToggleRight,
    accent: "text-cyan-400 ring-cyan-500/30",
    what:
      "Kho key-value cấu hình động theo tenant (bật/tắt tính năng, ngưỡng số…). Đọc nóng qua write-through cache Redis, nguồn chân lý ở Postgres.",
    store: "omni_admin.runtime_flag",
    steps: [
      { text: "Nhập flag_key, value, và value_type (int/bool/str/float/json)." },
      { text: "Apply → atomic TX: UPSERT flag + config_change_log + crat_outbox." },
      { text: "Sửa lại flag cũ sẽ tăng version và ghi lịch sử thay đổi." },
    ],
  },
  {
    href: "/admin/tenants",
    label: "Tenants & API Keys",
    icon: Building2,
    accent: "text-violet-400 ring-violet-500/30",
    what:
      "Quản lý tenant (multi-tenant isolation) và API key truy cập gateway. Key chỉ lưu sha256 hash — plaintext hiển thị DUY NHẤT một lần khi tạo.",
    store: "omni_admin.tenant · omni_admin.tenant_api_key",
    steps: [
      { text: "Create tenant: nhập tenant_id + display_name." },
      { text: "Issue API key: copy ngay plaintext — đóng dialog là mất, không xem lại được." },
      { text: "Revoke key hoặc suspend tenant khi cần thu hồi quyền." },
    ],
    note: "Bí mật chỉ qua env + K8s Secret; UI không bao giờ lưu plaintext.",
  },
];

export default function GuidePage() {
  return (
    <div className="mx-auto max-w-3xl p-5 space-y-6 font-sans">
      <header className="space-y-2">
        <h1 className="text-lg font-semibold tracking-tight text-zinc-100">Hướng dẫn sử dụng Admin Console</h1>
        <p className="text-[12px] leading-relaxed text-zinc-400">
          Trang Admin chỉ phụ trách <span className="text-zinc-200">cấu hình hệ thống</span> (autonomy &amp;
          tenant). Quan sát/telemetry (workers, KPI, CRAT, deploy) nằm ở Ops Console riêng. Mọi thay đổi đều ghi
          atomic vào Postgres <code className="text-amber-400">omni_admin</code> + audit log + CRAT outbox.
        </p>
      </header>

      <div className="grid grid-cols-1 gap-2 rounded-lg border border-zinc-800 bg-zinc-900/40 p-3 sm:grid-cols-2">
        <div className="flex items-start gap-2">
          <Database className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
          <p className="text-[11px] leading-relaxed text-zinc-400">
            <span className="text-zinc-200">Source-of-truth:</span> Postgres <code className="text-amber-400">omni_admin</code>.
            Redis chỉ là write-through cache cho hot-path.
          </p>
        </div>
        <div className="flex items-start gap-2">
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-sky-400" />
          <p className="text-[11px] leading-relaxed text-zinc-400">
            <span className="text-zinc-200">Fail-closed:</span> Postgres lỗi → rollback, cache không đụng. Nâng
            quyền luôn cần xác nhận 2 bước.
          </p>
        </div>
      </div>

      <div className="space-y-4">
        {ENTRIES.map((e) => (
          <article key={e.href} className="rounded-lg border border-zinc-800 bg-zinc-900/30 p-4">
            <div className="flex items-center gap-3">
              <span className={`flex h-9 w-9 items-center justify-center rounded-md bg-zinc-950 ring-1 ${e.accent}`}>
                <e.icon className="h-4 w-4" />
              </span>
              <h2 className="flex-1 text-[13px] font-semibold text-zinc-100">{e.label}</h2>
              <Link
                href={e.href}
                className="flex items-center gap-1 text-[10px] text-zinc-500 transition-colors hover:text-cyan-400"
              >
                mở panel <ArrowRight className="h-3 w-3" />
              </Link>
            </div>

            <p className="mt-3 text-[11px] leading-relaxed text-zinc-400">{e.what}</p>

            <ol className="mt-3 space-y-1.5">
              {e.steps.map((s, i) => (
                <li key={i} className="flex gap-2 text-[11px] leading-relaxed text-zinc-300">
                  <span className="mt-px font-mono text-[9px] text-zinc-600">{String(i + 1).padStart(2, "0")}</span>
                  <span>{s.text}</span>
                </li>
              ))}
            </ol>

            <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-zinc-800/60 pt-2.5">
              <span className="flex items-center gap-1 text-[9px] text-zinc-600">
                <Database className="h-3 w-3" />
                {e.store}
              </span>
              {e.note && <span className="ml-auto text-[9px] italic text-zinc-500">{e.note}</span>}
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}
