import type { NavItem } from "@aoip/shared-types";

// GOVERNING RULE (2026-07-01): navigation CHỈ phản ánh capability backend/runtime đã tồn tại.
// Portal là "operational projection" của runtime — KHÔNG phải product portal. Không license/
// billing/CRM/deployment/policy-editor/onboarding-wizard trừ khi backend đã production-ready.
// Mỗi mục map tới một nguồn runtime thật: Overview(/overview) · Agents(registry) ·
// Understanding(mission/facts) · Missions(mission runtime) · Incidents(Trace Spine) ·
// Human Inbox(pending approvals/questions) · Audit(trace/audit chain). Account ở header.
// `implemented=false` = read-projection chưa expose (backend có, projection đang tới) — KHÔNG
// phải product domain giả. Route product-domain cũ giữ trên đĩa nhưng KHÔNG liệt kê (dọn sau).
export const PROVIDER_NAV: NavItem[] = [
  { label: "Overview", href: "/", implemented: true },
  { label: "Agents", href: "/agents", implemented: false, slice: "Fleet projection" },
  { label: "Understanding", href: "/understanding", implemented: false, slice: "Understanding projection" },
  { label: "Missions", href: "/missions", implemented: false, slice: "Mission projection" },
  { label: "Incidents", href: "/incidents", implemented: false, slice: "Incident timeline projection" },
  { label: "Human Inbox", href: "/human-inbox", implemented: false, slice: "Human Inbox projection" },
  { label: "Audit", href: "/audit", implemented: false, slice: "Audit & trace projection" },
];

/** Lý do khe hở cho một read-projection chưa expose (nêu rõ nguồn runtime backing). */
export function stubReason(item: NavItem): string {
  return `«${item.label}» sẽ hiển thị khi read-projection tương ứng được expose từ runtime ` +
    `(${item.slice ?? "projection"}). Portal chỉ chiếu capability backend đã có — không tạo ` +
    `product state ở frontend. Xem docs/plans/aoip-provider-portal-slices.md.`;
}
