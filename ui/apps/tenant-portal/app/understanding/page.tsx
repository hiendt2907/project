import { headers } from "next/headers";
import { backendGet } from "@aoip/api-client";
import { Card, MetricStat } from "@aoip/ui-kit";
import { backendConfig } from "@/lib/config";

export default async function TenantUnderstandingPage() {
  const resp = await backendGet(backendConfig, "/understanding", (await headers()).get("cookie") ?? "");
  if (!resp.ok) return <Card error><div className="aoip-state">Không tải được System Twin ({resp.status}).</div></Card>;
  const body = await resp.json() as { tenants: Array<{ tenant_id: string; twin: { entity_count: number; fact_count: number; relationship_count: number }; unknown_count: number; question_count: number }> };
  const tenant = body.tenants[0];
  if (!tenant) return <><h1>System Twin</h1><Card><div className="aoip-state">Chưa có dữ liệu discovery.</div></Card></>;
  return <><h1>System Twin</h1><p className="aoip-muted">Mô hình hệ thống được dựng từ bằng chứng agent của tenant.</p>
    <div className="aoip-grid"><MetricStat label="Entities" value={tenant.twin.entity_count} /><MetricStat label="Facts" value={tenant.twin.fact_count} /><MetricStat label="Relationships" value={tenant.twin.relationship_count} /><MetricStat label="Unknowns" value={tenant.unknown_count} /><MetricStat label="Câu hỏi" value={tenant.question_count} /></div>
    <Card><div className="aoip-muted">Tenant: {tenant.tenant_id}. Chi tiết topology và competency được hiển thị từ projection backend, không phải dữ liệu giả.</div></Card>
  </>;
}
