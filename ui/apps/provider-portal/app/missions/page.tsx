import { headers } from "next/headers";
import { Card } from "@aoip/ui-kit";
import { fetchMissions } from "@/lib/missions";
import { PageIntro } from "@/components/PageIntro";

export default async function Page() {
  const missions = await fetchMissions((await headers()).get("cookie") ?? "");
  return <>
    <PageIntro title="Nhiệm vụ vận hành" lead="Các mission đang chạy trên runtime AOIP, theo dõi tiến độ và bước tiếp theo từ nguồn thật." />
    <Card>
      {!missions ? <div className="aoip-state">Không tải được mission projection.</div> : missions.length === 0 ?
        <div className="aoip-state">Chưa có mission nào được ghi nhận.</div> : missions.map((m) => (
          <div className="aoip-row" key={`${m.tenant_id}:${m.mission_id}`}>
            <span>{m.tenant_id} · {m.goal}<br /><small>{m.mission_id}</small></span>
            <span>{m.state} · {Math.round(m.completion * 100)}%<br /><small>{m.next_action ?? m.last_activity ?? ""}</small></span>
          </div>
        ))}
    </Card>
  </>;
}
