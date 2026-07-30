// Nút "Test lại" trên trang /diagnostics — inject một sự cố mẫu qua đường
// remote-agent THẬT (target:"remote" → handle_remote_agent_evidence → vòng chẩn
// đoán đa lượt: RAG → LLM → CRAT → Telegram). Không phải mock: cùng pipeline sự
// cố thật đi qua. Chỉ khác một điều — nó KHÔNG dừng service vật lý trên VM (pod
// gateway không có quyền chạm máy khách); muốn tác động VM thật dùng script
// scripts/diag-test-vm.sh trên host. Xem /simulate/{lane} src/gateway/routes/simulate.py.
import { NextRequest, NextResponse } from "next/server";
import { resolveSession } from "@aoip/auth-client";
import { backendConfig } from "@/lib/config";

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

// Kịch bản → lane simulator. Chỉ các lane remote sinh vòng chẩn đoán đa lượt.
const SCENARIO_LANE: Record<string, string> = {
  service: "state", // service down (systemd) → domain=service
  network: "state", // mất cổng lắng nghe → domain=network
  disk: "resource", // đĩa đầy → domain=storage
  cpu: "resource", // tải CPU → domain=os_host
};

function errorResponse(status: number, detail: string) {
  return NextResponse.json({ error: detail }, { status });
}

export async function POST(request: NextRequest) {
  const session = await resolveSession(backendConfig, request.headers.get("cookie") ?? "");
  if (session.status !== "authenticated") return errorResponse(401, "Cần đăng nhập");
  if (!GATEWAY_URL) return errorResponse(502, "OMNI_GATEWAY_URL chưa cấu hình");

  const body = await request.json().catch(() => null);
  const scenario = String(body?.scenario ?? "").trim();
  const tenantId = String(body?.tenant_id ?? "default").trim() || "default";
  const lane = SCENARIO_LANE[scenario];
  if (!lane) {
    return errorResponse(400, `scenario không hợp lệ: ${scenario || "(trống)"}`);
  }

  try {
    const response = await fetch(`${GATEWAY_URL}/simulate/${lane}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {}),
      },
      body: JSON.stringify({
        target: "remote",
        tenant_id: tenantId,
        agent_id: `${tenantId}_diag-test`,
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    const data = await response.json().catch(() => ({}));
    return NextResponse.json(data, { status: response.status });
  } catch {
    return errorResponse(502, "Gateway không phản hồi");
  }
}
