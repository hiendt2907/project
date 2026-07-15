import { headers } from "next/headers";
import { resolveSession } from "@aoip/auth-client";
import { Card, MetricStat } from "@aoip/ui-kit";
import type { TenantIdentity } from "@aoip/shared-types";
import { backendConfig } from "@/lib/config";

export default async function TenantHome() {
  const session = await resolveSession(backendConfig, (await headers()).get("cookie") ?? "");
  if (session.status !== "authenticated") return null;
  const me = session.identity as TenantIdentity;
  return <>
    <h1>Tổng quan vận hành</h1>
    <p className="aoip-muted">Đây là góc nhìn của tổ chức {me.active_tenant}. Omni cung cấp bộ não điều phối; các Agent là tai mắt trên hệ thống của bạn.</p>
    <div className="aoip-grid" data-testid="tenant-overview-grid">
      <MetricStat label="Tổ chức" value={me.active_tenant} />
      <div data-testid="roles"><MetricStat label="Vai trò" value={me.roles.join(", ") || "-"} /></div>
      <MetricStat label="Quyền" value={me.permissions.length} />
    </div>
    <Card><h2>Bắt đầu vận hành</h2><p className="aoip-muted">Mở Agent của tôi để kiểm tra kết nối, System Twin để xem Omni đã hiểu hệ thống đến đâu, hoặc Sự cố để theo dõi timeline và phê duyệt.</p></Card>
  </>;
}
