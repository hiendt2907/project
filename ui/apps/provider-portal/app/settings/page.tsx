import { headers } from "next/headers";
import { Card } from "@aoip/ui-kit";
import { fetchSettings } from "@/lib/settings";
import { fetchMutationToggle } from "@/lib/mutation";
import { SettingsPanel } from "./SettingsPanel";
import { MutationToggle } from "./MutationToggle";
import "./settings.css";
import { PageIntro } from "@/components/PageIntro";

export default async function ProviderSettingsPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const result = await fetchSettings(cookieHeader);

  if (result.status === "error") {
    return (
      <Card error>
        <div className="aoip-k err">Không tải được Settings</div>
        <div className="aoip-state" data-testid="settings-error">
          Backend trả mã {result.code || "không phản hồi"}. Thử tải lại trang.
        </div>
      </Card>
    );
  }

  const { tenants, agent_credentials } = result.data;
  const mutationByTenant = await Promise.all(tenants.map((tenant) => fetchMutationToggle(tenant.tenant_id)));
  return (
    <>
      <PageIntro
        title="Cài đặt kết nối"
        lead="Nơi «tuyển» agent mới vào hệ thống và thu hồi quyền của agent cũ. Muốn giám sát thêm một máy chủ của khách hàng: phát hành mã kết nối dùng-một-lần tại đây, đưa cho người cài đặt. Máy nào không còn tin tưởng: thu hồi quyền — hiệu lực ngay lập tức."
        terms={[
          { term: "Mã kết nối (enroll token)", meaning: "Mã dùng đúng MỘT lần để máy mới gia nhập. Dùng xong hoặc bị lộ thì vô giá trị — không thể dùng lại." },
          { term: "Thu hồi (revoke)", meaning: "Cắt quyền truy cập của một agent ngay lập tức — máy đó không gửi/nhận được gì nữa cho tới khi được cấp lại." },
        ]}
      />
      {tenants.length === 0 ? (
        <div className="aoip-state" data-testid="settings-empty">
          Chưa có tenant nào provision.
        </div>
      ) : (
        <>
          <Card>
            <div className="aoip-k">Quyền vận hành</div>
            <div className="aoip-muted">Mặc định khóa. Bật tenant switch chỉ ghi nhận quyền; master kill-switch vẫn có thể khóa toàn hệ thống.</div>
            {tenants.map((tenant, index) => (
              <MutationToggle key={tenant.tenant_id} tenantId={tenant.tenant_id} initial={mutationByTenant[index]} />
            ))}
          </Card>
          <Card>
            <SettingsPanel tenants={tenants} agentCredentials={agent_credentials} />
          </Card>
        </>
      )}
    </>
  );
}
