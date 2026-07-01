import Link from "next/link";
import { headers } from "next/headers";
import { resolveSession } from "@aoip/auth-client";
import { browserPaths } from "@aoip/api-client";
import { Card, Chips, Header, KeyVal, LogoutButton, StateScreen } from "@aoip/ui-kit";
import type { TenantIdentity } from "@aoip/shared-types";
import { backendConfig, TENANT_API_BASE } from "@/lib/config";

// Server component Tenant Portal. Disclosure RIÊNG của tenant: active_tenant (backend
// suy từ membership, KHÔNG nhận tenant_id từ client) + danh sách membership. Ẩn hạ tầng
// vận hành nền tảng (khác Provider Portal). Backend enforce quyền server-side.
export default async function TenantHome() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const session = await resolveSession(backendConfig, cookieHeader);

  if (session.status === "unauthenticated") {
    return (
      <Shell>
        <StateScreen kind="info" title="Tenant Operations"
          message="Bạn chưa đăng nhập. Xác thực qua nhà cung cấp OIDC để tiếp tục."
          loginHref={browserPaths.login} />
      </Shell>
    );
  }
  if (session.status === "forbidden") {
    return (
      <Shell>
        <StateScreen kind="error" title="403 · Không có quyền"
          message="Tài khoản của bạn không có membership hợp lệ cho Tenant Portal."
          action={<LogoutButton logoutPath={browserPaths.logout(TENANT_API_BASE)} />} />
      </Shell>
    );
  }
  if (session.status === "expired") {
    return (
      <Shell>
        <StateScreen kind="error" title="Phiên hết hạn"
          message="Phiên máy chủ đã hết hạn hoặc bị thu hồi. Vui lòng đăng nhập lại."
          loginHref={browserPaths.login} />
      </Shell>
    );
  }

  const me = session.identity as TenantIdentity;
  const memberChips = Object.entries(me.memberships).map(([t, r]) => `${t} · ${r}`);
  return (
    <Shell whoami={me.subject}>
      <Card>
        <KeyVal label="Danh tính" testid="subject">{me.subject}</KeyVal>
        <KeyVal label="Portal">Tenant Operations ({me.kind})</KeyVal>
        <KeyVal label="Tổ chức đang hoạt động (server-side)" testid="active-tenant">
          {me.active_tenant}
        </KeyVal>
        <KeyVal label="Thành viên (không do client chọn)" testid="memberships">
          <Chips items={memberChips} />
        </KeyVal>
        <KeyVal label="Vai trò" testid="roles"><Chips items={me.roles} /></KeyVal>
        <KeyVal label="Quyền (backend-enforced)" testid="perms"><Chips items={me.permissions} /></KeyVal>
        <div className="aoip-center">
          <LogoutButton logoutPath={browserPaths.logout(TENANT_API_BASE)} />
        </div>
      </Card>
      <div className="aoip-muted">
        Chỉ dữ liệu của tổ chức bạn; tenant khác bị chặn ở backend (REST + realtime).
      </div>
    </Shell>
  );
}

function Shell({ children, whoami }: { children: React.ReactNode; whoami?: string }) {
  return (
    <>
      <Header title="AOIP · Your Operations" whoami={whoami}
        nav={<Link href="/" aria-current="page">Tổng quan</Link>} />
      <main className="aoip-main">{children}</main>
    </>
  );
}
