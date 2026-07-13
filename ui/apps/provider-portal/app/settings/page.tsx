import { headers } from "next/headers";
import { Card } from "@aoip/ui-kit";
import { fetchSettings } from "@/lib/settings";
import { SettingsPanel } from "./SettingsPanel";
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
        <Card>
          <SettingsPanel tenants={tenants} agentCredentials={agent_credentials} />
        </Card>
      )}
    </>
  );
}
