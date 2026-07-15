// Design system dùng chung — CHỈ presentation. Không chứa authz/disclosure của portal.
import * as React from "react";

export { LogoutButton } from "./LogoutButton";
export { Sidebar } from "./Sidebar";

export function Header({ title, whoami, context, nav }: {
  title: string; whoami?: string; context?: string; nav?: React.ReactNode;
}) {
  return (
    <header className="aoip-header">
      <div className="aoip-header-title">
        <span className="aoip-header-mark" aria-hidden>◈</span>
        <div>
          <h1>{title}</h1>
          <span className="aoip-header-context">CONTROL ROOM · PROVIDER</span>
        </div>
      </div>
      {nav ? <nav className="aoip-nav" aria-label="Điều hướng chính">{nav}</nav> : null}
      <div className="aoip-header-right">
        <span className="aoip-view-label">OPERATOR VIEW</span>
        <span className="aoip-header-divider" aria-hidden />
        {context ? <span className="aoip-muted" data-testid="active-tenant">{context}</span> : null}
        <span className="aoip-muted" data-testid="whoami">{whoami ?? ""}</span>
      </div>
    </header>
  );
}

export function Card({ children, error }: { children: React.ReactNode; error?: boolean }) {
  return <div className={error ? "aoip-card err" : "aoip-card"}>{children}</div>;
}

export function KeyVal({ label, children, testid }: {
  label: string; children: React.ReactNode; testid?: string;
}) {
  return (
    <>
      <div className="aoip-k">{label}</div>
      <div className="aoip-v" data-testid={testid}>{children}</div>
    </>
  );
}

export function Chips({ items }: { items: string[] }) {
  if (!items || items.length === 0) return <span className="aoip-muted">—</span>;
  return <>{items.map((x) => <span className="aoip-chip" key={x}>{x}</span>)}</>;
}

/** CTA đăng nhập — luôn trỏ tới backend /auth/login (same-origin qua proxy). */
export function LoginCTA({ label, loginHref }: { label: string; loginHref: string }) {
  return (
    <div className="aoip-center">
      <a className="aoip-btn" href={loginHref}>{label}</a>
    </div>
  );
}

/** Màn trạng thái không-authenticated (401/403/expired/signed-out). */
export function StateScreen({ kind, title, message, loginHref, action }: {
  kind: "info" | "error";
  title: string; message: string; loginHref?: string; action?: React.ReactNode;
}) {
  return (
    <Card error={kind === "error"}>
      <div className={kind === "error" ? "aoip-k err" : "aoip-k"}>{title}</div>
      <div className="aoip-state">{message}</div>
      {loginHref ? <LoginCTA label="Đăng nhập" loginHref={loginHref} /> : null}
      {action ? <div className="aoip-center">{action}</div> : null}
    </Card>
  );
}

/** Khung app: header + sidebar điều hướng + vùng nội dung. Presentation-only. */
export function AppShell({ title, whoami, context, whoamiHref, sidebar, children }: {
  title: string; whoami?: string; whoamiHref?: string;
  context?: string;
  sidebar: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className="aoip-shell">
      <Header title={title} whoami={whoami} context={context}
        nav={whoami && whoamiHref
          ? <a className="aoip-account" href={whoamiHref}>Tài khoản</a>
          : undefined} />
      <div className="aoip-body">
        {sidebar}
        <main className="aoip-content">
          <div className="aoip-content-inner">{children}</div>
        </main>
      </div>
    </div>
  );
}

/** Ô số liệu control-tower: nhãn + giá trị lớn; hoặc note khe-hở nếu chưa có nguồn. */
export function MetricStat({ label, value, hint }: {
  label: string; value: React.ReactNode; hint?: string;
}) {
  return (
    <div className="aoip-stat" data-testid={`stat-${label}`}>
      <div className="aoip-k">{label}</div>
      <div className="aoip-stat-v">{value}</div>
      {hint ? <div className="aoip-muted">{hint}</div> : null}
    </div>
  );
}

/** Note "chưa khả dụng — lý do khe hở nguồn" (KHÔNG hiển thị số giả). */
export function Unavailable({ reason }: { reason: string }) {
  return (
    <span className="aoip-unavail" data-testid="unavailable" title={reason}>
      chưa khả dụng
    </span>
  );
}

/** Trang mục điều hướng chưa triển khai — nêu rõ sub-slice sẽ lấp. */
export function SectionStub({ title, reason }: { title: string; reason: string }) {
  return (
    <Card>
      <div className="aoip-k">{title}</div>
      <div className="aoip-state" data-testid="section-unavailable">
        Chức năng này chưa khả dụng trong slice hiện tại.
      </div>
      <div className="aoip-muted">{reason}</div>
    </Card>
  );
}
