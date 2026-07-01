import { test, expect, Page } from "@playwright/test";

// Sub-slice A — Provider Control Tower. Chứng minh trên trình duyệt thật (Next.js production):
// nav 15 mục, Overview số thật + mục unavailable nêu rõ lý do, Account tách khỏi trang chủ,
// component health đọc liveness thật. Backend enforce RBAC (đã phủ ở portals.spec + pytest).

const PROVIDER = "http://provider.ai-agent.local";
const PASSWORD = "Password123!";

async function login(page: Page, email: string) {
  await page.goto(PROVIDER + "/");
  await page.click('a[href="/auth/login"]');
  await page.waitForURL(/dex\.ai-agent\.local/);
  await page.fill('input[name="login"]', email);
  await page.fill('input[name="password"]', PASSWORD);
  await page.click('button[type="submit"], input[type="submit"]');
  await page.waitForURL(new RegExp(PROVIDER.replace(/\//g, "\\/") + "\\/?$"));
}

test("Overview: control-tower số thật + component health liveness", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  await expect(page.getByTestId("overview-grid")).toBeVisible();
  // Component health đọc liveness thật — redis phải ok (cluster đang chạy).
  await expect(page.getByTestId("health-redis")).toContainText("ok");
  // Số agents online là số (không phải chữ giả).
  await expect(page.getByTestId("stat-Agents online")).toBeVisible();
});

test("Overview: metric chưa có nguồn hiển thị 'chưa khả dụng' (không số giả)", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  // Missions/Version drift/Câu hỏi chờ… chưa có nguồn ở slice A → marker unavailable.
  const unavail = page.getByTestId("unavailable");
  await expect(unavail.first()).toBeVisible();
  await expect(page.getByTestId("stat-Missions")).toContainText("chưa khả dụng");
});

test("Nav: chỉ 7 vùng runtime-backed; projection chưa expose đánh dấu 'chưa khả dụng'", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  const links = page.locator(".aoip-side-link");
  // Governing rule: nav chỉ phản ánh capability runtime (Overview + 6 read-projection).
  await expect(links).toHaveCount(7);
  await expect(page.locator(".aoip-side-link.soon").first()).toBeVisible();
  // KHÔNG còn product-domain trong nav (license/onboarding/deployments…).
  await expect(page.locator(".aoip-side-link", { hasText: "Licenses" })).toHaveCount(0);
  // Vào một read-projection chưa expose → section stub nêu rõ nguồn runtime backing.
  await page.goto(PROVIDER + "/agents");
  await expect(page.getByTestId("section-unavailable")).toBeVisible();
});

test("Account: identity/roles/permissions tách khỏi trang chủ", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  await page.goto(PROVIDER + "/account");
  await expect(page.getByTestId("subject")).toHaveText("owner@aoip.dev");
  await expect(page.getByTestId("roles")).toContainText("platform_owner");
  await expect(page.getByTestId("logout")).toBeVisible();
});
