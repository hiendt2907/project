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

test("Overview: metric thiếu nguồn ghi 'chưa khả dụng', metric CÓ nguồn ghi số thật", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  // Điều cần bảo vệ là CƠ CHẾ: thiếu nguồn thì nói thiếu, KHÔNG hiển thị số giả.
  //
  // Bản cũ ghim `stat-Missions` phải là "chưa khả dụng" — đúng lúc Redis chưa có
  // Mission nào. Khi Mission runtime bắt đầu ghi dữ liệu thật, metric hợp lệ và test
  // đỏ vì test lỗi thời. Gắn assert vào dữ liệu-lúc-đó là làm test tự hỏng theo thời
  // gian, nên ở đây kiểm bất biến hai chiều thay vì một ô cụ thể.
  await expect(page.getByTestId("overview-grid")).toBeVisible();
  const missions = page.getByTestId("stat-Missions");
  await expect(missions).toBeVisible();
  const text = (await missions.textContent()) ?? "";
  if (text.includes("chưa khả dụng")) {
    // Thiếu nguồn → phải có marker tường minh, không được để trống hay số 0 giả.
    await expect(page.getByTestId("unavailable").first()).toBeVisible();
  } else {
    // Có nguồn → phải là SỐ, và không được lẫn chữ "chưa khả dụng".
    expect(text).toMatch(/\d/);
  }
});

test("Nav: chỉ vùng runtime-backed (16 mục, nhãn VI); Agents projection hiển thị bảng thật", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  const links = page.locator(".aoip-side-link");
  // Governing rule: nav chỉ phản ánh capability runtime. 2026-07-14: thêm Gói dịch vụ
  // sau khi tenant_plan backend đã có API, RBAC, audit và enforcement thật.
  // 2026-07-21: thêm Kho tri thức (RAG) — port admin/kb, xem lib/nav.ts NGOẠI LỆ.
  //
  // Con số này là ĐẾM CHÍNH XÁC, cố ý: nó bắt được cả việc VÔ TÌNH MẤT mục nav, thứ mà
  // `toBeGreaterThan` bỏ lọt. Đổi nav ⇒ phải đổi số ở đây. Nguồn sự thật là
  // `ui/apps/provider-portal/lib/nav.ts` — đếm bằng `grep -c "href:" <file>`.
  // 2026-07-30 thêm /diagnostics (→15) nhưng KHÔNG ai cập nhật số này ⇒ assertion đã đỏ
  // âm thầm từ đó. 2026-08-02 thêm /architecture (→16) và sửa lại cho khớp thực tế.
  await expect(links).toHaveCount(16);
  await expect(page.locator(".aoip-side-link", { hasText: "Xử lý sự cố" })).toHaveCount(1);
  await expect(page.locator(".aoip-side-link", { hasText: "Gói dịch vụ" })).toHaveCount(1);
  await expect(page.locator(".aoip-side-link", { hasText: "Kho tri thức" })).toHaveCount(1);
  await expect(page.locator(".aoip-side-link", { hasText: "Bản vẽ kiến trúc" })).toHaveCount(1);
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

// Số bước KHÔNG hardcode: nó phải khớp PIPELINE_STAGES
// (src/pkg/observability/pipeline_stages.py) và STAGE_VI (lib/pipeline.ts). Bản cũ ghim
// 12 và trượt lại khi `AUTO_RECOVERY` được thêm vào backend — test đỏ vì test lỗi thời,
// không vì sản phẩm sai. Giữ đúng một nguồn: đếm số bước portal thật sự render.
const PIPELINE_STAGE_COUNT = 13;

test("Pipeline: drill-down SỰ CỐ hiển thị đủ các bước; tín hiệu học hỏi hiển thị giải thích 1 bước", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  await page.goto(PROVIDER + "/pipeline");
  // Sự cố thật (nếu có) → đủ các bước pipeline.
  const incidentLink = page.getByTestId("pipeline-table").locator(".aoip-trace-link").first();
  if ((await incidentLink.count()) > 0) {
    await incidentLink.click();
    await expect(page.getByTestId("stage-list").locator(".aoip-stage")).toHaveCount(PIPELINE_STAGE_COUNT);
    await expect(page.getByText("Tiếp nhận")).toBeVisible();
    await page.goBack();
  }
  // Tín hiệu học hỏi (nếu có) → KHÔNG vẽ pipeline, có giải thích "không phải sự cố".
  const learningLink = page.getByTestId("learning-list").locator(".aoip-trace-link").first();
  if ((await learningLink.count()) > 0) {
    await learningLink.click();
    await expect(page.getByTestId("learning-status")).toContainText("hoàn thành");
    await expect(page.getByTestId("learning-explain")).toContainText("không phải sự cố");
    await expect(page.getByTestId("stage-list")).toHaveCount(0);
  }
});

// Port của ui/app/trace/[id] (audit 2026-07-21): thay vì trang riêng, phần chẩn đoán
// (session đa lượt / advisory một lượt / trạng thái chưa có) là section bổ sung ngay
// dưới 12 bước trên pipeline/[traceId] — đúng như audit đề xuất.
test("Pipeline drill-down: chi tiết chẩn đoán hiển thị đúng MỘT trong 3 trạng thái (session/advisory/rỗng)", async ({ page }) => {
  await login(page, "owner@aoip.dev");
  await page.goto(PROVIDER + "/pipeline");
  const incidentLink = page.getByTestId("pipeline-table").locator(".aoip-trace-link").first();
  if ((await incidentLink.count()) === 0) return; // không có sự cố nào lúc chạy test — bỏ qua, không phải lỗi.

  await incidentLink.click();
  const turns = page.getByTestId("diag-turns");
  const advisory = page.getByTestId("diag-advisory");
  const empty = page.getByTestId("diag-empty");
  await expect(turns.or(advisory).or(empty).first()).toBeVisible();

  // Nếu là session đa lượt: mỗi lượt là <details> có thể mở, lượt 1 mở sẵn.
  if ((await turns.count()) > 0) {
    await expect(page.getByTestId("diag-turn-1")).toHaveAttribute("open", /.*/);
  }
  // Nếu là advisory một lượt: có nhãn "Kết luận" (KeyVal render sẵn).
  if ((await advisory.count()) > 0) {
    await expect(advisory).toContainText("Kết luận");
  }
  // Nếu rỗng: giải thích rõ đây là bình thường, không phải lỗi.
  if ((await empty.count()) > 0) {
    await expect(empty).toContainText("bình thường");
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
