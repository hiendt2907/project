import { SectionStub } from "@aoip/ui-kit";
import { PROVIDER_NAV, stubReason } from "@/lib/nav";

// Route khung production nhưng CHƯA triển khai ở Sub-slice A — đánh dấu unavailable + lý do
// khe hở (sub-slice sẽ lấp). KHÔNG dữ liệu giả. Backend chưa expose API tương ứng.
const ITEM = PROVIDER_NAV.find((n) => n.href === "/human-inbox")!;

export default function Page() {
  return <SectionStub title={ITEM.label} reason={stubReason(ITEM)} />;
}
