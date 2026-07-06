import { SectionStub } from "@aoip/ui-kit";

// KHÔNG có trong PROVIDER_NAV (xem lib/nav.ts GOVERNING RULE 2026-07-01) — user/RBAC management
// domain bị loại trừ khỏi provider portal có chủ đích cho tới khi có backend contract tenant-scoped.
export default function Page() {
  return (
    <SectionStub
      title="Users"
      reason="User/RBAC management cần backend contract tenant-scoped mới — ngoài phạm vi slice hiện tại. Xem docs/plans/aoip-provider-portal-slices.md."
    />
  );
}
