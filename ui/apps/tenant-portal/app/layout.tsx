import "@aoip/ui-kit/src/styles.css";
import type { Metadata } from "next";
import { headers } from "next/headers";
import { resolveSession } from "@aoip/auth-client";
import { browserPaths } from "@aoip/api-client";
import { AppShell, Sidebar, StateScreen, LogoutButton } from "@aoip/ui-kit";
import type { TenantIdentity } from "@aoip/shared-types";
import { backendConfig, TENANT_API_BASE } from "@/lib/config";
import { TENANT_NAV } from "@/lib/nav";

export const metadata: Metadata = {
  title: "AOIP · Your Operations",
  description: "AOIP Customer/Tenant Operations Portal",
};

// Route tree + navigation RIÊNG của Tenant Portal.
export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const session = await resolveSession(backendConfig, cookieHeader);
  let body: React.ReactNode;
  if (session.status === "authenticated") {
    const me = session.identity as TenantIdentity;
    body = (
      <AppShell title="AOIP · Your Operations" whoami={me.subject} context={me.active_tenant}
        sidebar={<Sidebar items={TENANT_NAV} />}>
        {children}
      </AppShell>
    );
  } else if (session.status === "forbidden") {
    body = <main className="aoip-main"><StateScreen kind="error" title="403 · Không có quyền"
      message="Tài khoản không có membership hợp lệ cho Tenant Portal."
      action={<LogoutButton logoutPath={browserPaths.logout(TENANT_API_BASE)} />} /></main>;
  } else if (session.status === "expired") {
    body = <main className="aoip-main"><StateScreen kind="error" title="Phiên hết hạn"
      message="Phiên máy chủ đã hết hạn hoặc bị thu hồi. Vui lòng đăng nhập lại."
      loginHref={browserPaths.login} /></main>;
  } else {
    body = <main className="aoip-main"><StateScreen kind="info" title="Tenant Operations"
      message="Bạn chưa đăng nhập. Xác thực qua nhà cung cấp OIDC để tiếp tục."
      loginHref={browserPaths.login} /></main>;
  }
  return (
    <html lang="vi">
      <body data-kind="tenant">{body}</body>
    </html>
  );
}
