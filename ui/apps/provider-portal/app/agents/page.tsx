import { SectionStub } from "@aoip/ui-kit";
import { PROVIDER_NAV, stubReason } from "@/lib/nav";

// Read-projection chiếu capability runtime đã có (chưa expose qua console API). KHÔNG dữ liệu
// giả, KHÔNG product state frontend — chỉ chỗ giữ điều hướng tới projection sắp expose.
const ITEM = PROVIDER_NAV.find((n) => n.href === "/agents")!;

export default function Page() {
  return <SectionStub title={ITEM.label} reason={stubReason(ITEM)} />;
}
