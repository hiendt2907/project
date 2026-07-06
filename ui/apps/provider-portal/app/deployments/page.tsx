import { SectionStub } from "@aoip/ui-kit";

// KHÔNG có trong PROVIDER_NAV (xem lib/nav.ts GOVERNING RULE 2026-07-01) — deployment-center bị
// loại trừ khỏi provider portal có chủ đích: omni-ui /deploy hiển thị component trạng thái
// cluster-wide (không tenant-scoped, không phải projection phù hợp cho SaaS đa tenant) và nút
// "Rollout"/"Restart" hiện chỉ trả về mock acknowledgement (route.ts POST không trigger K8s thật) —
// không có backend contract thật để port. Giữ nguyên loại trừ theo quyết định kiến trúc.
export default function Page() {
  return (
    <SectionStub
      title="Deployments"
      reason="Deployment Center là cluster-wide admin view, không phải projection tenant-scoped của provider portal — loại trừ có chủ đích theo GOVERNING RULE (lib/nav.ts). Xem docs/plans/aoip-provider-portal-slices.md."
    />
  );
}
