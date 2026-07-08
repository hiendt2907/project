import type { NavItem } from "@aoip/shared-types";

// GOVERNING RULE (2026-07-01): navigation CHỈ phản ánh capability backend/runtime đã tồn tại.
// Portal là "operational projection" của runtime — KHÔNG phải product portal. Không license/
// billing/CRM/deployment/policy-editor/onboarding-wizard trừ khi backend đã production-ready.
// Mỗi mục map tới một nguồn runtime thật: Overview(/overview) · Agents(registry) ·
// Understanding(mission/facts) · Missions(mission runtime) · Incidents(CRAT audit-chain, read-only
// — src/gateway/routes/siem.py `/siem/overview`, tenant-scoped) · Human Inbox(pending approvals/
// questions) · Audit(trace/audit chain). Account ở header.
// `implemented=false` = read-projection chưa expose (backend có, projection đang tới) — KHÔNG
// phải product domain giả. Route product-domain cũ giữ trên đĩa nhưng KHÔNG liệt kê (dọn sau):
// omni-ui /config/autonomy (policy write, global scope, no per-tenant auth) và /deploy
// (deployment-center, cluster-wide) — KHÔNG port, excluded above by design.
//
// NGOẠI LỆ (2026-07-08): Settings (agent enroll-token issue + credential revoke, IT-3 store)
// LÀ write/policy action nhưng KHÔNG bị loại theo rule trên — nó là thao tác vận hành thiết yếu
// per-tenant (không phải billing/CRM/global policy), scoped đúng RBAC (P_CHANGE_POLICY), audit
// đầy đủ qua config_change_log/CRAT. Portal không thêm domain "giả"; đây là surface thật cho
// một store đã production (AdminConfigRepo) mà trước đây chỉ curl tay được.
export const PROVIDER_NAV: NavItem[] = [
  { label: "Overview", href: "/", implemented: true },
  { label: "Agents", href: "/agents", implemented: true },
  { label: "Understanding", href: "/understanding", implemented: true },
  { label: "Missions", href: "/missions", implemented: false, slice: "Mission projection" },
  { label: "Incidents", href: "/incidents", implemented: true },
  { label: "Human Inbox", href: "/human-inbox", implemented: true },
  { label: "Settings", href: "/settings", implemented: true },
  { label: "Audit", href: "/audit", implemented: true },
];

/** Lý do khe hở cho một read-projection chưa expose (nêu rõ nguồn runtime backing). */
export function stubReason(item: NavItem): string {
  return `«${item.label}» sẽ hiển thị khi read-projection tương ứng được expose từ runtime ` +
    `(${item.slice ?? "projection"}). Portal chỉ chiếu capability backend đã có — không tạo ` +
    `product state ở frontend. Xem docs/plans/aoip-provider-portal-slices.md.`;
}
