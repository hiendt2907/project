import "@aoip/ui-kit/src/styles.css";
import type { Metadata } from "next";
import { headers } from "next/headers";
import { resolveSession } from "@aoip/auth-client";
import { browserPaths } from "@aoip/api-client";
import { AppShell, Sidebar, StateScreen, LogoutButton } from "@aoip/ui-kit";
import type { ProviderIdentity } from "@aoip/shared-types";
import { backendConfig, PROVIDER_API_BASE } from "@/lib/config";
import { PROVIDER_NAV } from "@/lib/nav";

export const metadata: Metadata = {
  title: "AOIP · Provider Operations",
  description: "AOIP Provider Operations Portal",
};

const TITLE = "AOIP · Provider Operations";

// Gate ở layout (server-side): phiên phân giải MỘT lần qua backend /me. Chưa auth → màn trạng
// thái (không nav, không render page con). Đã auth → AppShell (header + sidebar 15 mục) + page.
// Backend vẫn enforce quyền mọi request; nav chỉ là điều hướng, KHÔNG kiểm soát truy cập.
export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const session = await resolveSession(backendConfig, cookieHeader);

  let body: React.ReactNode;
  if (session.status === "authenticated") {
    const me = session.identity as ProviderIdentity;
    body = (
      <AppShell title={TITLE} whoami={me.subject} whoamiHref="/account"
        sidebar={<Sidebar items={PROVIDER_NAV} />}>
        {children}
      </AppShell>
    );
  } else if (session.status === "forbidden") {
    body = (
      <main className="aoip-main">
        <StateScreen kind="error" title="403 · Không có quyền"
          message="Tài khoản của bạn không có vai trò hợp lệ cho Provider Portal."
          action={<LogoutButton logoutPath={browserPaths.logout(PROVIDER_API_BASE)} />} />
      </main>
    );
  } else if (session.status === "expired") {
    body = (
      <main className="aoip-main">
        <StateScreen kind="error" title="Phiên hết hạn"
          message="Phiên máy chủ đã hết hạn hoặc bị thu hồi. Vui lòng đăng nhập lại."
          loginHref={browserPaths.login} />
      </main>
    );
  } else {
    body = (
      <main className="aoip-main">
        <StateScreen kind="info" title="Provider Operations"
          message="Bạn chưa đăng nhập. Xác thực qua nhà cung cấp OIDC để tiếp tục."
          loginHref={browserPaths.login} />
      </main>
    );
  }

  return (
    <html lang="vi">
      <body data-kind="provider">{body}</body>
    </html>
  );
}
