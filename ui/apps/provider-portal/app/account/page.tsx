import { headers } from "next/headers";
import { resolveSession } from "@aoip/auth-client";
import { browserPaths } from "@aoip/api-client";
import { Card, Chips, KeyVal, LogoutButton } from "@aoip/ui-kit";
import type { ProviderIdentity } from "@aoip/shared-types";
import { backendConfig, PROVIDER_API_BASE } from "@/lib/config";

// Account / Profile — danh tính, vai trò, quyền (backend-enforced) + đăng xuất. Đây là nơi
// disclosure identity của Provider Portal (chuyển từ trang chủ cũ sang, theo yêu cầu Sub-slice A).
export default async function ProviderAccountPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const session = await resolveSession(backendConfig, cookieHeader);

  if (session.status !== "authenticated") {
    return (
      <Card error>
        <div className="aoip-k err">Phiên không hợp lệ</div>
        <div className="aoip-state">Vui lòng đăng nhập lại để xem tài khoản.</div>
      </Card>
    );
  }

  const me = session.identity as ProviderIdentity;
  return (
    <Card>
      <KeyVal label="Danh tính" testid="subject">{me.subject}</KeyVal>
      <KeyVal label="Portal">Provider Operations ({me.kind})</KeyVal>
      <KeyVal label="Vai trò" testid="roles"><Chips items={me.roles} /></KeyVal>
      <KeyVal label="Quyền (backend-enforced)" testid="perms"><Chips items={me.permissions} /></KeyVal>
      <div className="aoip-center">
        <LogoutButton logoutPath={browserPaths.logout(PROVIDER_API_BASE)} />
      </div>
      <div className="aoip-muted">
        Quyền do backend cưỡng chế trên từng request; ẩn menu KHÔNG phải kiểm soát truy cập.
      </div>
    </Card>
  );
}
