import { test as setup, expect } from "@playwright/test";

const STORAGE_STATE = "e2e/.auth/state.json";

setup("login via NextAuth credentials form", async ({ page }) => {
  const username = process.env.E2E_USERNAME;
  const password = process.env.E2E_PASSWORD;
  if (!username || !password) {
    throw new Error(
      "E2E_USERNAME / E2E_PASSWORD chưa được set. Lấy từ: kubectl -n multi-agent get secret omni-ui-secrets",
    );
  }

  await page.goto("/login");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"));

  const cookies = await page.context().cookies();
  expect(
    cookies.some((c) => c.name.includes("next-auth.session-token")),
  ).toBe(true);

  await page.context().storageState({ path: STORAGE_STATE });
});
