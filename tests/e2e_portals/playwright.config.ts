import { defineConfig } from "@playwright/test";

// Traefik LB IP (OrbStack). Không sửa /etc/hosts: Chromium tự map host → LB qua
// --host-resolver-rules. Đây là DEV cluster (production-shaped development).
const LB = process.env.AOIP_LB_IP ?? "192.168.139.2";

export default defineConfig({
  testDir: "./specs",
  timeout: 45_000,
  expect: { timeout: 10_000 },
  retries: 1,
  reporter: [["list"]],
  use: {
    ignoreHTTPSErrors: true,
    launchOptions: {
      args: [
        `--host-resolver-rules=MAP provider.ai-agent.local ${LB}, ` +
          `MAP tenant.ai-agent.local ${LB}, MAP dex.ai-agent.local ${LB}`,
      ],
    },
  },
});
