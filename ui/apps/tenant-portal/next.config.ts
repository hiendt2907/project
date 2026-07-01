import type { NextConfig } from "next";

// Tenant Portal — entry point / route tree / CSP / deploy RIÊNG (tách hẳn Provider).
// Same-origin ở tầng INGRESS: /auth/* + /api/tenant/v1/* route thẳng tới FastAPI backend
// (cùng host portal). Cookie riêng aoip_tenant_session. Next chỉ phục vụ UI + /me SSR.
const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: require("path").join(__dirname, "../../"),
  transpilePackages: [
    "@aoip/ui-kit",
    "@aoip/api-client",
    "@aoip/auth-client",
    "@aoip/observability",
    "@aoip/shared-types",
  ],
};

export default nextConfig;
