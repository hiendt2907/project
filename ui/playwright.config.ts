import { defineConfig, devices } from "@playwright/test";

/**
 * E2E chạy trực tiếp lên omni-ui thật (K8s) qua kubectl port-forward:
 *   kubectl -n multi-agent port-forward svc/omni-ui 18081:80
 * Hostname được map về 127.0.0.1 bằng Chromium host-resolver-rules để
 * NextAuth cookie/realm middleware nhận đúng Host (omni.ai-agent.local).
 * Mọi assertion HTTP đi qua browser (page/fetch) — KHÔNG dùng
 * APIRequestContext vì resolver rules không áp cho Node.
 */
const baseURL = process.env.E2E_BASE_URL ?? "http://omni.ai-agent.local:18081";
const { hostname } = new URL(baseURL);

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  outputDir: "test-results",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: {
      args: [`--host-resolver-rules=MAP ${hostname} 127.0.0.1`],
    },
  },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    {
      name: "chromium",
      dependencies: ["setup"],
      use: {
        ...devices["Desktop Chrome"],
        storageState: "e2e/.auth/state.json",
      },
    },
  ],
});
