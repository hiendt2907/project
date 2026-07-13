import { headers } from "next/headers";
import { Card, MetricStat } from "@aoip/ui-kit";
import { PageIntro } from "@/components/PageIntro";
import { fetchTenants } from "@/lib/operations";

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
      ) : result.data.tenants.length === 0 ? (
        <Card>
          <div className="aoip-state" data-testid="tenants-empty">
            Chưa có khách hàng nào có hồ sơ sự cố trong hệ thống theo dõi.
          </div>
        </Card>
      ) : (
        <div className="aoip-grid" data-testid="tenants-grid">
          {result.data.tenants.map((t) => (
            <MetricStat
              key={t.tenant}
              label={t.tenant}
              value={`${t.incidents} sự cố trong hồ sơ`}
              hint="Số sự cố có hồ sơ theo dõi cho khách hàng này"
            />
          ))}
        </div>
      )}
    </>
  );
}
