import { headers } from "next/headers";
import { Card, MetricStat } from "@aoip/ui-kit";
import { fetchAgents } from "@/lib/agents";
import { AgentsTable } from "./AgentsTable";
import { PageIntro } from "@/components/PageIntro";

export default async function ProviderAgentsPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const result = await fetchAgents(cookieHeader);

  if (result.status === "error") {
    return (
      <Card error>
        <div className="aoip-k err">Không tải được Agents</div>
        <div className="aoip-state" data-testid="agents-error">
          Backend trả mã {result.code || "không phản hồi"}. Thử tải lại trang.
        </div>
      </Card>
    );
  }

  const { summary, agents } = result.data;
  return (
    <>
      <PageIntro
        title="Agents tại khách hàng"
        lead="Agent là phần mềm nhỏ cài trên từng máy chủ của khách hàng — nó là «tai mắt» của Omni: quan sát, thu thập dấu hiệu và báo về, không tự ý can thiệp. Trang này cho biết từng agent còn liên lạc đều không và có đang chạy đúng phiên bản phần mềm mới nhất không."
        terms={[
          { term: "Online / Offline", meaning: "Agent gửi tín hiệu «tôi còn sống» mỗi 30 giây. Mất tín hiệu quá lâu = offline, cần kiểm tra máy hoặc mạng." },
          { term: "Drift (lệch phiên bản)", meaning: "Agent đang chạy phần mềm khác với bản chính thức đã phát hành — như nhân viên làm việc theo quy trình cũ. Cần cập nhật." },
          { term: "Runtime", meaning: "Loại chương trình agent đang chạy (bản mới «employee» hay bản cũ «legacy»)." },
        ]}
      />
      <div className="aoip-grid" data-testid="agents-summary">
        <MetricStat label="Tổng số agent" value={summary.total} hint="Số máy chủ khách hàng đang được giám sát" />
        <MetricStat label="Đang liên lạc (online)" value={summary.online} hint="Có tín hiệu trong 2 phút gần nhất" />
        <MetricStat label="Chậm liên lạc (stale)" value={summary.stale} hint="Lâu chưa có tín hiệu — nên để mắt" />
        <MetricStat label="Mất liên lạc (offline)" value={summary.offline} hint="Không còn tín hiệu — cần kiểm tra" />
        <MetricStat label="Lệch phiên bản (drift)" value={summary.drifted} hint="Đang chạy bản phần mềm cũ/không đúng chuẩn" />
      </div>

      <Card>
        <AgentsTable agents={agents} />
      </Card>
    </>
  );
}
