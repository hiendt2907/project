import { SectionStub } from "@aoip/ui-kit";

// KHÔNG có trong PROVIDER_NAV (xem lib/nav.ts GOVERNING RULE 2026-07-01) — System Twin đã hiển thị
// một phần ở Understanding (entity graph); route riêng này chưa có nav slot xác nhận.
export default function Page() {
  return (
    <SectionStub
      title="Systems"
      reason="System Twin đã có ở Understanding (entity graph); route riêng chưa có nav slot xác nhận. Xem docs/plans/aoip-provider-portal-slices.md."
    />
  );
}
