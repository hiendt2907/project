import { SectionStub } from "@aoip/ui-kit";

// KHÔNG có trong PROVIDER_NAV (xem lib/nav.ts GOVERNING RULE 2026-07-01) — license/billing domain
// bị loại trừ khỏi provider portal có chủ đích (portal chỉ chiếu runtime capability, không CRM/billing).
export default function Page() {
  return (
    <SectionStub
      title="Licenses"
      reason="License/billing chưa có backend runtime tương ứng — ngoài phạm vi operational projection hiện tại. Xem docs/plans/aoip-provider-portal-slices.md."
    />
  );
}
