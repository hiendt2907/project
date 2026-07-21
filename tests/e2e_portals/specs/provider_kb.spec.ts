import { test, expect, Page } from "@playwright/test";

// RAG Knowledge-Base page (port of legacy ui/app/admin/kb) — provider-only, cluster-global
// vendor knowledge feeding the diagnosis LLM (src/gateway/routes/kb.py, no tenant_id).
// Proxied through /api/gateway/kb, /api/gateway/kb/[collection]/[id] (app/api/gateway/kb/*).

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

test("Kho tri thức: trang có PageIntro + panel tải dữ liệu thật từ gateway", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  await page.goto(PROVIDER + "/kb");
  await expect(page.getByTestId("page-intro")).toBeVisible();
  await expect(page.getByTestId("kb-panel")).toBeVisible();
  // Không bao giờ kẹt ở "đang tải…" — hoặc có bảng, hoặc empty-state, hoặc lỗi rõ ràng.
  await expect(
    page.getByTestId("kb-table").or(page.getByTestId("kb-empty")).or(page.getByTestId("kb-error")).first(),
  ).toBeVisible();
  await expect(page.getByTestId("kb-summary")).toBeVisible();
});

test("Kho tri thức: thêm một mục mới, tìm thấy qua ô tìm kiếm, rồi xoá", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  await page.goto(PROVIDER + "/kb");

  const marker = `e2e-kb-${Date.now()}`;
  await page.getByTestId("kb-add-toggle").click();
  const form = page.getByTestId("kb-add-form");
  await expect(form).toBeVisible();
  await form.getByPlaceholder("Tiêu đề *").fill(marker);
  await form.getByPlaceholder("Nhà cung cấp (Kubernetes, Redis…)").fill("e2e-vendor");
  await form
    .getByPlaceholder("Nội dung tri thức * — cách điều tra nguyên nhân gốc và phạm vi ảnh hưởng")
    .fill("Created by tests/e2e_portals/specs/provider_kb.spec.ts — safe to delete.");
  await form.getByRole("button", { name: "Nhúng & lưu" }).click();

  // Embedding round-trips through Ollama — give it real headroom before asserting.
  await expect(page.getByTestId("kb-msg")).toContainText("Đã thêm", { timeout: 30_000 });

  await page.getByTestId("kb-search").fill(marker);
  const row = page.locator(`[data-testid^="kb-item-vendor_knowledge-"]`, { hasText: marker });
  await expect(row).toHaveCount(1);

  page.once("dialog", (dialog) => void dialog.accept());
  await row.getByRole("button", { name: /^Xoá/ }).click();
  await expect(row).toHaveCount(0);
});
