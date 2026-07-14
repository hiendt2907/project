"use client";
// Điều hướng dọc — client component để tô mục đang active (usePathname). CHỈ presentation:
// nav items truyền vào props; mục implemented=false hiển thị nhãn "chưa khả dụng" (không ẩn,
// không giả). Ẩn/hiện KHÔNG phải authz — backend enforce mọi route.
import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { NavItem } from "@aoip/shared-types";

export function Sidebar({ items }: { items: NavItem[] }) {
  const pathname = usePathname();
  return (
    <nav className="aoip-side" aria-label="Điều hướng Provider">
      <div className="aoip-side-brand">
        <div className="aoip-side-mark">AOIP</div>
        <div className="aoip-side-caption">CONTROL ROOM</div>
      </div>
      <ul>
        {items.map((it) => {
          const active = pathname === it.href;
          const cls = [
            "aoip-side-link",
            active ? "active" : "",
            it.implemented ? "" : "soon",
          ].join(" ").trim();
          return (
            <li key={it.href}>
              <Link href={it.href} className={cls}
                aria-current={active ? "page" : undefined}
                aria-disabled={it.implemented ? undefined : true}>
                <span className="aoip-side-label">
                  <span className="aoip-side-icon" aria-hidden>{iconFor(it.href)}</span>
                  <span>{it.label}</span>
                </span>
                {it.implemented ? null : <span className="aoip-soon-tag">chưa khả dụng</span>}
              </Link>
            </li>
          );
        })}
      </ul>
      <div className="aoip-side-footer">
        <span className="aoip-live-dot" aria-hidden />
        <span><strong>Provider Operations</strong><small>Runtime projection</small></span>
      </div>
    </nav>
  );
}

function iconFor(href: string): string {
  const icons: Record<string, string> = {
    "/": "⌂", "/tenants": "▦", "/pipeline": "⌁", "/kpi": "▥",
    "/agents": "◉", "/understanding": "◇", "/incidents": "△",
    "/operations": "!", "/human-inbox": "□", "/settings": "⚙", "/audit": "≡",
  };
  return icons[href] ?? "·";
}
