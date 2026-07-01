import { test, expect, Page, BrowserContext } from "@playwright/test";

// AOIP Slice 0 — browser E2E cho HAI ứng dụng Next.js production (provider-portal /
// tenant-portal). Backend AOIP là auth authority DUY NHẤT; portal tiêu thụ cookie
// HttpOnly host-scoped riêng. CSP nonce của Next bật. DEV cluster (HTTP + user tĩnh).

const PROVIDER = "http://provider.ai-agent.local";
const TENANT = "http://tenant.ai-agent.local";
const PASSWORD = "Password123!";

async function oidcLogin(page: Page, portal: string, email: string) {
  await page.goto(portal + "/");
  await page.click('a[href="/auth/login"]');
  await page.waitForURL(/dex\.ai-agent\.local/);
  await page.fill('input[name="login"]', email);
  await page.fill('input[name="password"]', PASSWORD);
  await page.click('button[type="submit"], input[type="submit"]');
  await page.waitForURL(new RegExp(portal.replace(/\//g, "\\/") + "\\/?$"));
}

test("provider (Next): css áp dụng, /me SSR resolve, identity render, CSP nonce bật", async ({ page }) => {
  const csp: string[] = [];
  page.on("console", (m) => {
    if (m.text().includes("Content Security Policy")) csp.push(m.text());
  });
  await oidcLogin(page, PROVIDER, "owner@aoip.dev");
  await expect(page.getByTestId("whoami")).toHaveText("owner@aoip.dev");
  // Sub-slice A: trang chủ là Provider Control Tower (Overview), KHÔNG còn là identity card.
  await expect(page.getByTestId("overview-grid")).toBeVisible();
  // Identity chuyển sang /account.
  await page.goto(PROVIDER + "/account");
  await expect(page.getByTestId("roles")).toContainText("platform_owner");
  await expect(page.getByTestId("perms")).toContainText("view");
  // stylesheet ngoài (ui-kit) áp dụng thật
  const border = await page.locator("header").evaluate((el) => getComputedStyle(el).borderBottomColor);
  expect(border).not.toBe("rgb(0, 0, 0)");
  expect(csp).toEqual([]); // 0 lỗi CSP (nonce hợp lệ)
  // không token trong browser storage
  const store = await page.evaluate(() => JSON.stringify(localStorage) + JSON.stringify(sessionStorage));
  expect(store).not.toMatch(/id_token|access_token|eyJ/);
});

test("provider (Next): logout thu hồi phiên máy chủ", async ({ page }) => {
  await oidcLogin(page, PROVIDER, "owner@aoip.dev");
  await page.goto(PROVIDER + "/account");  // logout nằm ở trang Account (Sub-slice A)
  await page.getByTestId("logout").click();
  await expect(page.locator("body")).toContainText("chưa đăng nhập");
  const me = await page.request.get(PROVIDER + "/api/provider/v1/me");
  expect(me.status()).toBe(401);
});

test("provider (Next): trạng thái 401 (unauth) render khi chưa đăng nhập", async ({ page }) => {
  await page.goto(PROVIDER + "/");
  await expect(page.locator("body")).toContainText("chưa đăng nhập");
  await expect(page.locator('a[href="/auth/login"]')).toBeVisible();
});

test("tenant (Next): đăng nhập, active org server-side, chỉ thấy tenant mình", async ({ page }) => {
  await oidcLogin(page, TENANT, "sre@acme.dev");
  await expect(page.getByTestId("whoami")).toHaveText("sre@acme.dev");
  await expect(page.getByTestId("active-tenant")).toHaveText("acme");
  await expect(page.getByTestId("roles")).toContainText("tenant_owner");
  const inc = await page.request.get(TENANT + "/api/tenant/v1/incidents");
  expect((await inc.json()).tenant).toBe("acme");
});

test("cross-portal: provider user KHÔNG vào Tenant Portal (callback 403)", async ({ page }) => {
  await page.goto(TENANT + "/");
  await page.click('a[href="/auth/login"]');
  await page.waitForURL(/dex\.ai-agent\.local/);
  await page.fill('input[name="login"]', "owner@aoip.dev");
  await page.fill('input[name="password"]', PASSWORD);
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().includes("/auth/callback")),
    page.click('button[type="submit"], input[type="submit"]'),
  ]);
  expect(resp.status()).toBe(403);
});

test("trạng thái 403 (forbidden) render khi session sai loại portal", async ({ browser }) => {
  const ctx: BrowserContext = await browser.newContext();
  const p = await ctx.newPage();
  await oidcLogin(p, PROVIDER, "owner@aoip.dev");
  const cookies = await ctx.cookies(PROVIDER);
  const providerSid = cookies.find((c) => c.name === "aoip_provider_session")!.value;
  await ctx.addCookies([{
    name: "aoip_tenant_session", value: providerSid,
    domain: "tenant.ai-agent.local", path: "/", httpOnly: true, sameSite: "Lax",
  }]);
  await p.goto(TENANT + "/");
  await expect(p.locator("body")).toContainText("Không có quyền");
  await ctx.close();
});

test("cookie isolation: hai portal Next cùng browser KHÔNG đụng cookie", async ({ browser }) => {
  const ctx = await browser.newContext();
  const p1 = await ctx.newPage();
  await oidcLogin(p1, PROVIDER, "owner@aoip.dev");
  const p2 = await ctx.newPage();
  await oidcLogin(p2, TENANT, "sre@globex.dev");
  await p1.reload();
  await expect(p1.getByTestId("whoami")).toHaveText("owner@aoip.dev");
  await expect(p2.getByTestId("whoami")).toHaveText("sre@globex.dev");
  const prov = await ctx.cookies(PROVIDER);
  const ten = await ctx.cookies(TENANT);
  expect(prov.some((c) => c.name === "aoip_provider_session")).toBeTruthy();
  expect(ten.some((c) => c.name === "aoip_tenant_session")).toBeTruthy();
  await ctx.close();
});

test("session expiry: phiên bị thu hồi → request kế 401 (không tin claim frontend)", async ({ page }) => {
  await oidcLogin(page, TENANT, "sre@globex.dev");
  await expect(page.getByTestId("whoami")).toHaveText("sre@globex.dev");
  await page.request.post(TENANT + "/api/tenant/v1/logout", { headers: { "X-AOIP-CSRF": "1" } });
  const me = await page.request.get(TENANT + "/api/tenant/v1/me");
  expect(me.status()).toBe(401);
  await page.reload();
  await expect(page.locator("body")).toContainText("chưa đăng nhập");
});
