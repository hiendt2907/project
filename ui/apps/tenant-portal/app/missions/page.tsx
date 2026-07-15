import { headers } from "next/headers";
import { Card } from "@aoip/ui-kit";
import { fetchMissions } from "@/lib/missions";

export default async function TenantMissionsPage() {
  const missions = await fetchMissions((await headers()).get("cookie") ?? "");
  return <>
    <h1>Nhiệm vụ vận hành</h1>
    <p className="aoip-muted">Tiến độ các nhiệm vụ hệ thống trong tổ chức của bạn.</p>
    <Card>{!missions ? <div className="aoip-state">Không tải được nhiệm vụ.</div> : missions.length === 0 ?
      <div className="aoip-state">Chưa có nhiệm vụ nào.</div> : missions.map((m) =>
        <div className="aoip-row" key={m.mission_id}>
          <span>{m.goal}<br /><small>{m.mission_id}</small></span>
          <span>{m.state} · {Math.round(m.completion * 100)}%<br /><small>{m.next_action ?? m.last_activity ?? ""}</small></span>
        </div>)}
    </Card>
  </>;
}
