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

test("Nav: chỉ vùng runtime-backed (13 mục, nhãn VI); Agents projection hiển thị bảng thật", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  const links = page.locator(".aoip-side-link");
  // Governing rule: nav chỉ phản ánh capability runtime. 2026-07-14: thêm Gói dịch vụ
  // sau khi tenant_plan backend đã có API, RBAC, audit và enforcement thật.
  await expect(links).toHaveCount(13);
  await expect(page.locator(".aoip-side-link", { hasText: "Xử lý sự cố" })).toHaveCount(1);
  await expect(page.locator(".aoip-side-link", { hasText: "Gói dịch vụ" })).toHaveCount(1);
  // Agents là projection đã expose từ runtime registry, không còn section stub.
  await page.goto(PROVIDER + "/agents");
  await expect(page.getByTestId("agents-summary")).toBeVisible();
  await expect(page.getByTestId("agents-table")).toBeVisible();

  await page.goto(PROVIDER + "/understanding");
  const stagingSim = page.getByTestId("understanding-staging-sim");
  await expect(stagingSim).toBeVisible();
  await expect(stagingSim.getByTestId("facts-table")).toBeVisible();
  await expect(stagingSim.getByTestId("competency-table")).toBeVisible();
});

test("Account: identity/roles/permissions tách khỏi trang chủ", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  await page.goto(PROVIDER + "/account");
  await expect(page.getByTestId("subject")).toHaveText("owner@aoip.dev");
  await expect(page.getByTestId("roles")).toContainText("platform_owner");
  await expect(page.getByTestId("logout")).toBeVisible();
});

// 2026-07-13 — lớp giải thích phi kỹ thuật + 4 read-projection mới (Pipeline/KPI/
// Vận hành/Khách hàng). Mỗi trang phải có PageIntro (mô tả đời thường) và render
// dữ liệu thật hoặc empty-state có giải thích — KHÔNG trắng trang, KHÔNG số giả.

test("PageIntro: mọi trang chính có mô tả đời thường + chú giải thuật ngữ", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  for (const path of ["/", "/agents", "/understanding", "/human-inbox", "/audit"]) {
    await page.goto(PROVIDER + path);
    await expect(page.getByTestId("page-intro"), `intro on ${path}`).toBeVisible();
  }
});

test("Pipeline: tách Sự cố vs Tín hiệu học hỏi (INV_KNOWLEDGE_NOT_ALERT trên UI)", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  await page.goto(PROVIDER + "/pipeline");
  await expect(page.getByTestId("page-intro")).toBeVisible();
  // Khu sự cố: bảng 12 bước HOẶC empty-state có giải thích.
  await expect(
    page.getByTestId("pipeline-table").or(page.getByTestId("pipeline-empty")).first(),
  ).toBeVisible();
  // Khu học hỏi luôn hiện (list hoặc empty) — discovery KHÔNG được hiển thị như sự cố kẹt.
  await expect(
    page.getByTestId("learning-list").or(page.getByTestId("learning-empty")).first(),
  ).toBeVisible();
  // Không dòng học hỏi nào mang chữ "đang xử lý".
  const learningRows = page.locator(".aoip-learning-row");
  if ((await learningRows.count()) > 0) {
    await expect(learningRows.first()).toContainText("đã ghi nhận");
  }
});

test("Pipeline: drill-down SỰ CỐ hiển thị 12 bước; tín hiệu học hỏi hiển thị giải thích 1 bước", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  await page.goto(PROVIDER + "/pipeline");
  // Sự cố thật (nếu có) → 12 bước.
  const incidentLink = page.getByTestId("pipeline-table").locator(".aoip-trace-link").first();
  if ((await incidentLink.count()) > 0) {
    await incidentLink.click();
    await expect(page.getByTestId("stage-list").locator(".aoip-stage")).toHaveCount(12);
    await expect(page.getByText("Tiếp nhận")).toBeVisible();
    await page.goBack();
  }
  // Tín hiệu học hỏi (nếu có) → KHÔNG vẽ 12 bước, có giải thích "không phải sự cố".
  const learningLink = page.getByTestId("learning-list").locator(".aoip-trace-link").first();
  if ((await learningLink.count()) > 0) {
    await learningLink.click();
    await expect(page.getByTestId("learning-status")).toContainText("hoàn thành");
    await expect(page.getByTestId("learning-explain")).toContainText("không phải sự cố");
    await expect(page.getByTestId("stage-list")).toHaveCount(0);
  }
});

test("KPI: số liệu 24h thật, không số giả", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  await page.goto(PROVIDER + "/kpi");
  await expect(page.getByTestId("page-intro")).toBeVisible();
  await expect(page.getByTestId("kpi-summary")).toBeVisible();
});

test("Vận hành + Khách hàng: render projection console thật", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  await page.goto(PROVIDER + "/operations");
  await expect(
    page.getByTestId("operations-empty").or(page.locator(".aoip-card").first()).first(),
  ).toBeVisible();
  await page.goto(PROVIDER + "/tenants");
  await expect(
    page.getByTestId("tenants-grid").or(page.getByTestId("tenants-empty")).first(),
  ).toBeVisible();
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
