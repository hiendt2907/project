import { test, expect, type Page } from "@playwright/test";

const TENANT = process.env.E2E_TENANT ?? "staging-sim";
const ALLOW_WRITE = process.env.E2E_ALLOW_WRITE === "1";

async function gotoUnderstanding(page: Page): Promise<void> {
  await page.goto(`/understanding?tenant=${TENANT}`);
  await expect(
    page.getByRole("heading", { name: "System Understanding" }),
  ).toBeVisible();
}

test.describe("read flow", () => {
  test("renders readiness, entities, unknowns, questions sections", async ({ page }) => {
    await gotoUnderstanding(page);
    const main = page.getByRole("main");
    await expect(main.getByText("Understanding Readiness")).toBeVisible();
    await expect(main.getByText("System Twin Entities")).toBeVisible();
    await expect(main.getByText(/Open Unknowns \(\d+\)/)).toBeVisible();
    await expect(main.getByText(/Questions \(\d+ pending\)/)).toBeVisible();
    // Twin thật của tenant lab phải có entity — trống là regression dữ liệu
    await expect(
      page.getByRole("button", { name: /^(host|svc):/ }).first(),
    ).toBeVisible({ timeout: 15_000 });
  });

  test("renders the system diagram as Mermaid SVG", async ({ page }) => {
    await gotoUnderstanding(page);
    const card = page.getByTestId("diagram-card");
    await expect(card.getByText("System Diagram").first()).toBeVisible();
    // Tenant lab đã có diagram versioned trong Redis — thiếu SVG là regression
    await expect(card.getByText(/^v\d+$/)).toBeVisible({ timeout: 15_000 });
    await expect(card.getByTestId("mermaid-svg").first()).toBeVisible({ timeout: 15_000 });
    // Cả 3 loại diagram (component / API sequence / business flow) đều render
    await expect(card.getByTestId("mermaid-svg")).toHaveCount(3, { timeout: 15_000 });
    expect(await card.locator("svg").count()).toBeGreaterThanOrEqual(3);
  });

  test("selecting an entity loads its competency matrix", async ({ page }) => {
    await gotoUnderstanding(page);
    const entity = page.getByRole("button", { name: /^(host|svc):/ }).first();
    await entity.waitFor({ timeout: 15_000 });
    const entityId = (await entity.textContent())?.trim() ?? "";
    await entity.click();
    await expect(
      page.getByText(`Competency Matrix — ${entityId}`),
    ).toBeVisible();
    await expect(
      page.getByRole("columnheader", { name: "Facet" }),
    ).toBeVisible({ timeout: 15_000 });
    // Ít nhất một facet có state badge hợp lệ
    await expect(
      page
        .locator("tbody tr td span")
        .filter({ hasText: /^(VERIFIED|CLAIMED|OBSERVED|CONTRADICTED|STALE|UNKNOWN|NOT_APPLICABLE)$/ })
        .first(),
    ).toBeVisible();
  });
});

test.describe("write flow — answer question", () => {
  test("answer a PENDING question and see ANSWERED badge", async ({ page }) => {
    test.skip(!ALLOW_WRITE, "write flow chỉ chạy khi E2E_ALLOW_WRITE=1 (mutates lab state)");
    await gotoUnderstanding(page);
    const answerBtn = page.getByRole("button", { name: "Answer" }).first();
    const hasPending = await answerBtn
      .waitFor({ timeout: 15_000 })
      .then(() => true)
      .catch(() => false);
    test.skip(!hasPending, `tenant ${TENANT} không còn PENDING question nào`);

    // Sau submit, load() re-render row (Answer button biến mất) — so sánh
    // số badge ANSWERED trước/sau thay vì bám vào row cũ.
    const answeredBadge = page.getByText("ANSWERED", { exact: true });
    const answeredBefore = await answeredBadge.count();
    await answerBtn.click();
    await page.getByPlaceholder("Your name / role").fill("playwright-e2e");
    await page
      .getByPlaceholder(/Answer \(becomes a CLAIMED fact/)
      .fill(`e2e runtime answer for tenant ${TENANT}`);
    await page.getByRole("button", { name: "Submit answer" }).click();
    await expect
      .poll(() => answeredBadge.count(), { timeout: 15_000 })
      .toBeGreaterThan(answeredBefore);
  });

  test("proxy rejects invalid question_id with 400 (validated before forward)", async ({ page }) => {
    await gotoUnderstanding(page);
    const result = await page.evaluate(async () => {
      const res = await fetch("/api/onboarding/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question_id: "bad id!",
          answered_by: "playwright-e2e",
          value: "should be rejected at proxy",
        }),
      });
      return { status: res.status, body: await res.json().catch(() => null) };
    });
    expect(result.status).toBe(400);
  });
});

test.describe("auth boundary", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("unauthenticated page visit redirects to /login", async ({ page }) => {
    await page.goto(`/understanding?tenant=${TENANT}`).catch(() => {
      // Ops-realm redirect trỏ về public host KHÔNG port (Ingress port 80) —
      // qua port-forward navigation cuối có thể không tải được; chỉ cần
      // xác nhận middleware đã đẩy sang /login.
    });
    await page.waitForURL((url) => url.pathname.startsWith("/login"));
    expect(new URL(page.url()).pathname).toBe("/login");
  });

  test("unauthenticated answer POST returns 401", async ({ page }) => {
    await page.goto("/login");
    const status = await page.evaluate(async () => {
      const res = await fetch("/api/onboarding/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question_id: "x", answered_by: "a", value: "b" }),
      });
      return res.status;
    });
    expect(status).toBe(401);
  });
});
