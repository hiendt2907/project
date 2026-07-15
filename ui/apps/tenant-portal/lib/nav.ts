import type { NavItem } from "@aoip/shared-types";

export const TENANT_NAV: NavItem[] = [
  { label: "Tổng quan", href: "/", implemented: true },
  { label: "Agent của tôi", href: "/agents", implemented: true },
  { label: "Hệ thống (System Twin)", href: "/understanding", implemented: true },
  { label: "Sự cố", href: "/incidents", implemented: true },
  { label: "Phê duyệt", href: "/approvals", implemented: true },
  { label: "Nhiệm vụ vận hành", href: "/missions", implemented: true },
];
