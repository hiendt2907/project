import { SectionStub } from "@aoip/ui-kit";

// KHÔNG có trong PROVIDER_NAV (xem lib/nav.ts GOVERNING RULE 2026-07-01) — policy-editor bị loại
// trừ khỏi provider portal có chủ đích: omni-ui /config/autonomy đổi autonomy tier qua endpoint
// gateway `/autonomy/policy` KHÔNG tenant-scoped (áp dụng toàn cluster) và chưa có cơ chế
// auth/authorization theo tenant tương đương các write-action khác trong portal (vd Human Inbox
// answer). Port trực tiếp sẽ cho phép một tenant/operator đổi autonomy policy ảnh hưởng auto-execute
// pipeline của TẤT CẢ tenant — không an toàn. Cần backend contract mới (tenant-scoped policy write
// + authorization) trước khi triển khai slice này.
export default function Page() {
  return (
    <SectionStub
      title="Policies"
      reason="Autonomy policy editor cần backend contract tenant-scoped mới (gateway /autonomy/policy hiện là global, không theo tenant) — ngoài phạm vi slice hiện tại. Xem docs/plans/aoip-provider-portal-slices.md."
    />
  );
}
