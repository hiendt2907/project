// Nút "Test lại" trên trang /diagnostics — inject một sự cố mẫu qua đường
// remote-agent THẬT (target:"remote" → handle_remote_agent_evidence → vòng chẩn
// đoán đa lượt: RAG → LLM → CRAT → Telegram). Không phải mock: cùng pipeline sự
// cố thật đi qua. Chỉ khác một điều — nó KHÔNG dừng service vật lý trên VM (pod
// gateway không có quyền chạm máy khách); muốn tác động VM thật dùng script
// scripts/diag-test-vm.sh trên host. Xem /simulate/scenario/{scenario} trong
// src/gateway/routes/simulate.py — catalog kịch bản ở GET /simulate/scenarios.
import { NextRequest, NextResponse } from "next/server";
import { resolveSession } from "@aoip/auth-client";
import { backendConfig } from "@/lib/config";

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

// Kịch bản hợp lệ — PHẢI khớp `SCENARIO_KEYS` trong src/gateway/routes/simulate.py.
// Lịch sử: chỗ này từng map kịch bản sang "state"/"resource" rồi gọi `/simulate/{lane}`.
// Đó là `proof_lane` (trục B), không phải lane key của simulator (trục A: sys_resource/
// sys_hard_fail/app_http/siem_security) ⇒ gateway trả 400 "unknown lane" cho cả 4 nút.
// Nay gateway nhận thẳng kịch bản và tự khai domain, portal không đoán lane nữa.
const SCENARIOS = new Set(["service", "network", "disk", "cpu"]);

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
  if (!SCENARIOS.has(scenario)) {
    return errorResponse(400, `scenario không hợp lệ: ${scenario || "(trống)"}`);
  }

  try {
    const response = await fetch(`${GATEWAY_URL}/simulate/scenario/${scenario}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {}),
      },
      body: JSON.stringify({
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
