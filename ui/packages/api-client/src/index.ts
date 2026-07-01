// Tiện ích gọi API backend AOIP — provider-neutral, KHÔNG chứa chính sách portal.
// Backend là auth authority: mọi quyết định quyền ở server-side; client chỉ chuyển tiếp
// cookie phiên và đọc kết quả /me. KHÔNG lưu token ở browser.

/** Base URL backend cho SSR (gọi trong cluster). Trình duyệt KHÔNG dùng cái này —
 *  browser đi qua same-origin rewrite (/api/... , /auth/...) để cookie HttpOnly được gửi. */
export interface BackendConfig {
  /** URL nội bộ tới backend portal (vd http://aoip-provider-portal:8081). */
  ssrBaseUrl: string;
  /** Prefix API của portal (vd "/api/provider/v1"). */
  apiBase: string;
}

/** GET server-side, chuyển tiếp cookie phiên. Trả Response thô để caller phân trạng thái. */
export async function backendGet(
  cfg: BackendConfig,
  path: string,
  cookieHeader: string,
): Promise<Response> {
  return fetch(cfg.ssrBaseUrl + cfg.apiBase + path, {
    headers: { cookie: cookieHeader },
    cache: "no-store",
    redirect: "manual",
  });
}

/** Đường dẫn same-origin (browser) — được proxy sang backend qua next.config rewrites. */
export const browserPaths = {
  login: "/auth/login",
  logout: (apiBase: string) => `${apiBase}/logout`,
};
