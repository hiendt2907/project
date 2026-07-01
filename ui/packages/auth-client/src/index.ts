// Phân giải phiên SERVER-SIDE từ backend /me. KHÔNG có chính sách disclosure của portal —
// chỉ ánh xạ mã trạng thái HTTP → SessionState. Portal truyền apiBase riêng của mình.
// Backend vẫn là nơi enforce quyền; đây chỉ đọc kết quả để render.

import { backendGet, type BackendConfig } from "@aoip/api-client";
import type { Identity, SessionState } from "@aoip/shared-types";

/** Gọi /me server-side (chuyển tiếp cookie phiên host-scoped) → SessionState. */
export async function resolveSession(
  cfg: BackendConfig,
  cookieHeader: string,
): Promise<SessionState> {
  let resp: Response;
  try {
    resp = await backendGet(cfg, "/me", cookieHeader);
  } catch {
    return { status: "expired" };
  }
  if (resp.status === 401) return { status: "unauthenticated" };
  if (resp.status === 403) return { status: "forbidden" };
  if (!resp.ok) return { status: "expired" };
  const identity = (await resp.json()) as Identity;
  return { status: "authenticated", identity };
}
