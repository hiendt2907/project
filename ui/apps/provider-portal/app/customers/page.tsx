import { SectionStub } from "@aoip/ui-kit";

// KHÔNG có trong PROVIDER_NAV (xem lib/nav.ts GOVERNING RULE 2026-07-01) — CRM/tenant-registry
// domain bị loại trừ khỏi provider portal có chủ đích cho tới khi có backend contract tenant-scoped
// cho write-action (issue/revoke API key, suspend tenant). Ứng viên mạnh cho slice kế tiếp.
export default function Page() {
  return (
    <SectionStub
      title="Customers"
      reason="Tenant registry cần thiết kế bảo mật riêng cho write-action (issue API key, suspend) trước khi triển khai — ngoài phạm vi slice hiện tại. Xem docs/plans/aoip-provider-portal-slices.md."
    />
  );
}
