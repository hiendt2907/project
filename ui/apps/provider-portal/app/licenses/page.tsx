import { headers } from "next/headers";
import { Card } from "@aoip/ui-kit";
import { PageIntro } from "@/components/PageIntro";
import { fetchTenantPlan, fetchTenants } from "@/lib/operations";
import { PlanForm } from "./PlanForm";

export default function Page() {
  return <LicensesPage />;
}

async function LicensesPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const tenants = await fetchTenants(cookieHeader);
  if (tenants.status === "error") return <Card error>Không tải được danh sách khách hàng</Card>;

  const rows = await Promise.all(tenants.data.tenants.map(async (tenant) => ({
    tenant,
    plan: await fetchTenantPlan(cookieHeader, tenant.tenant_id ?? tenant.tenant),
  })));
  return (
    <>
      <PageIntro title="Gói dịch vụ" lead="Giới hạn và quyền vận hành của từng khách hàng. Các thay đổi được ghi audit và áp dụng vào enrollment cùng autonomy runtime." terms={[
        { term: "Autonomy ceiling", meaning: "Mức tự động cao nhất mà tenant được phép đạt; runtime vẫn có thể hạ mức theo độ tin cậy." },
        { term: "Agent limit", meaning: "Số agent active tối đa được phép cấp cho tenant." },
      ]} />
      {rows.length === 0 ? <Card>Chưa có khách hàng.</Card> : (
        <div className="aoip-grid" data-testid="licenses-grid">
          {rows.map(({ tenant, plan }) => {
            const tenantId = tenant.tenant_id ?? tenant.tenant;
            return <Card key={tenantId}>
              <div className="aoip-k">{tenant.display_name ?? tenantId}</div>
              {plan.status === "ok" ? <PlanForm tenantId={tenantId} initial={plan.data} /> : <div className="aoip-state">Chưa có cấu hình gói dịch vụ.</div>}
            </Card>;
          })}
        </div>
      )}
    </>
  );
}
