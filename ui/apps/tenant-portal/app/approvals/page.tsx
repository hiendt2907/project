import { headers } from "next/headers";
import { backendGet } from "@aoip/api-client";
import { Card } from "@aoip/ui-kit";
import { backendConfig } from "@/lib/config";

export default async function TenantApprovalsPage() {
  const resp = await backendGet(backendConfig, "/approvals", (await headers()).get("cookie") ?? "");
  if (!resp.ok) return <Card error><div className="aoip-state">Không tải được hàng đợi phê duyệt ({resp.status}).</div></Card>;
  const body = await resp.json() as { pending: string[] };
  return <><h1>Phê duyệt</h1><p className="aoip-muted">Các quyết định cần người có thẩm quyền xác nhận.</p><Card>
    {body.pending.length === 0 ? <div className="aoip-state">Không có phê duyệt đang chờ.</div> :
      <ul>{body.pending.map((id) => <li key={id}><code>{id}</code></li>)}</ul>}
  </Card></>;
}
