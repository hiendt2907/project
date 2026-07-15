import { headers } from "next/headers";
import { backendGet } from "@aoip/api-client";
import { Card } from "@aoip/ui-kit";
import { backendConfig } from "@/lib/config";

export default async function TenantIncidentsPage() {
  const resp = await backendGet(backendConfig, "/incidents", (await headers()).get("cookie") ?? "");
  if (!resp.ok) return <Card error><div className="aoip-state">Không tải được sự cố ({resp.status}).</div></Card>;
  const body = await resp.json() as { incidents: string[] };
  return <><h1>Sự cố</h1><p className="aoip-muted">Timeline sự cố thuộc tổ chức của bạn.</p><Card>
    {body.incidents.length === 0 ? <div className="aoip-state">Chưa có sự cố.</div> :
      <ul>{body.incidents.map((id) => <li key={id}><code>{id}</code></li>)}</ul>}
  </Card></>;
}
