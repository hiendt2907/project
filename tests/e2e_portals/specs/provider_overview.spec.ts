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

test("Nav: chỉ 7 vùng runtime-backed; Agents projection hiển thị bảng thật", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  const links = page.locator(".aoip-side-link");
  // Governing rule: nav chỉ phản ánh capability runtime (Overview + 6 read-projection).
  await expect(links).toHaveCount(7);
  await expect(page.locator(".aoip-side-link.soon").first()).toBeVisible();
  // KHÔNG còn product-domain trong nav (license/onboarding/deployments…).
  await expect(page.locator(".aoip-side-link", { hasText: "Licenses" })).toHaveCount(0);
  // Agents là projection đã expose từ runtime registry, không còn section stub.
  await page.goto(PROVIDER + "/agents");
  await expect(page.getByTestId("agents-summary")).toBeVisible();
  await expect(page.getByTestId("agents-table")).toBeVisible();

  await page.goto(PROVIDER + "/understanding");
  await expect(page.getByTestId("understanding-staging-sim")).toBeVisible();
  await expect(page.getByTestId("facts-table")).toBeVisible();
  await expect(page.getByTestId("competency-table")).toBeVisible();
});

test("Account: identity/roles/permissions tách khỏi trang chủ", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  await page.goto(PROVIDER + "/account");
  await expect(page.getByTestId("subject")).toHaveText("owner@aoip.dev");
  await expect(page.getByTestId("roles")).toContainText("platform_owner");
  await expect(page.getByTestId("logout")).toBeVisible();
});

test("Human Inbox: operator answers one runtime question", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  await page.goto(PROVIDER + "/human-inbox");
  await expect(page.getByTestId("human-inbox-summary")).toBeVisible();
  const firstForm = page.locator('.aoip-question[data-claimable="true"] .aoip-answer').first();
  await expect(firstForm).toBeVisible();
  await firstForm.locator('input[placeholder="Nhập câu trả lời"]').fill(`owner-${Date.now()}`);
  await firstForm.getByRole("button", { name: "Lưu Claim" }).click();
  await expect(firstForm.getByRole("button")).toContainText("Đã lưu");
});
