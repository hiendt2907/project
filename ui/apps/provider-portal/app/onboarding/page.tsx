import { SectionStub } from "@aoip/ui-kit";

// KHÔNG có trong PROVIDER_NAV (xem lib/nav.ts GOVERNING RULE 2026-07-01) — onboarding-wizard
// domain bị loại trừ khỏi provider portal có chủ đích (readiness đã cover một phần ở Understanding).
export default function Page() {
  return (
    <SectionStub
      title="Onboarding"
      reason="Onboarding wizard chưa production-ready cho provider portal; readiness đã hiển thị ở Understanding. Xem docs/plans/aoip-provider-portal-slices.md."
    />
  );
}
