import { headers } from "next/headers";
import { Card } from "@aoip/ui-kit";
import { PageIntro } from "@/components/PageIntro";
import { fetchEnvironments, fetchTenantPlan, fetchTenants } from "@/lib/operations";
import { EnvironmentForm } from "./EnvironmentForm";
import { TenantCreateForm } from "./TenantCreateForm";

export default async function TenantsPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const result = await fetchTenants(cookieHeader);

  return (
    <>
      <PageIntro
        title="Khách hàng"
        lead="Mỗi khách hàng (tenant) là một công ty có hệ thống đang được Omni trông coi. Trang này cho biết đang phục vụ những khách hàng nào và mỗi khách hàng có bao nhiêu sự cố được ghi nhận trong hồ sơ theo dõi."
        terms={[
          { term: "Tenant", meaning: "Một khách hàng — dữ liệu của từng khách hàng được tách riêng hoàn toàn, không lẫn sang nhau." },
        ]}
      />

      {result.status === "error" ? (
        <Card error>
          <div className="aoip-k err">Không tải được danh sách khách hàng</div>
          <div className="aoip-state" data-testid="tenants-error">
            Backend trả mã {result.code || "không phản hồi"}. Thử tải lại trang.
          </div>
        </Card>
      ) : (
        <>
          <Card><TenantCreateForm /></Card>
          {result.data.tenants.length === 0 ? <Card>
            <div className="aoip-state" data-testid="tenants-empty">Chưa có tenant nào.</div>
          </Card> : <div className="aoip-grid" data-testid="tenants-grid">
            {await Promise.all(result.data.tenants.map(async (t) => {
            const tenantId = t.tenant_id ?? t.tenant;
            const environments = await fetchEnvironments(cookieHeader, tenantId);
            const plan = await fetchTenantPlan(cookieHeader, tenantId);
            return (
              <Card key={tenantId}>
                <div className="aoip-k">{t.display_name ?? tenantId}</div>
                <div className="aoip-state">{t.status ?? "active"}</div>
                <div className="aoip-state">{t.incidents} sự cố trong hồ sơ</div>
                {plan.status === "ok" ? (
                  <div className="aoip-state" data-testid={`plan-${tenantId}`}>
                    Gói: {plan.data.plan_code} · agent tối đa {plan.data.agent_limit} ·
                    autonomy tối đa {plan.data.autonomy_ceiling} · lưu {plan.data.retention_days} ngày
                  </div>
                ) : <div className="aoip-state">Gói dịch vụ: chưa cấu hình</div>}
                {environments.status === "ok" ? (
                  <div className="aoip-state" data-testid={`environments-${tenantId}`}>
                    Môi trường: {environments.data.environments.length === 0
                      ? "chưa tạo"
                      : environments.data.environments.map((e) => `${e.display_name} (${e.status})`).join(", ")}
                  </div>
                ) : (
                  <div className="aoip-state">Môi trường: chưa kết nối được</div>
                )}
                <EnvironmentForm tenantId={tenantId} />
              </Card>
            );
            }))}
          </div>}
        </>
      )}
    </>
  );
}
