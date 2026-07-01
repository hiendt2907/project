import type { NextConfig } from "next";

// Provider Portal — entry point / route tree / CSP / deploy RIÊNG.
// Backend AOIP là auth authority DUY NHẤT. Same-origin đạt được ở tầng INGRESS:
// /auth/* và /api/provider/v1/* route thẳng tới FastAPI backend (cùng host portal),
// nên cookie HttpOnly (aoip_provider_session) được backend đặt trực tiếp. Next chỉ phục
// vụ UI (/) + gọi /me server-side qua AOIP_BACKEND_URL. Không NextAuth, không session 2.
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
