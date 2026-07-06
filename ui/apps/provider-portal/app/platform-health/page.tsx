import { SectionStub } from "@aoip/ui-kit";

// KHÔNG có trong PROVIDER_NAV (xem lib/nav.ts GOVERNING RULE 2026-07-01). Khác với
// policies/deployments, omni-ui /workers (Worker Fleet health) THỰC RA read-only và có thể
// tenant-scope được — cùng nguồn gateway `/agents` đã dùng ở /agents (lib/agents.ts). Chưa port
// vì thêm mục nav mới là quyết định kiến trúc (mở rộng PROVIDER_NAV) ngoài phạm vi 1
// capability/iteration hiện tại — không tự ý mở rộng nav. Ứng viên tốt cho slice kế tiếp.
export default function Page() {
  return (
    <SectionStub
      title="Platform Health"
      reason="Chưa có trong PROVIDER_NAV — cần quyết định kiến trúc để thêm mục nav mới (dữ liệu nguồn /agents đã sẵn có, read-only, portable). Xem docs/plans/aoip-provider-portal-slices.md."
    />
  );
}
