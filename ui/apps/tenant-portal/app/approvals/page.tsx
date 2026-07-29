import { headers } from "next/headers";
import { backendGet } from "@aoip/api-client";
import { Card } from "@aoip/ui-kit";
import { backendConfig } from "@/lib/config";

/**
 * Hàng đợi phê duyệt của tenant.
 *
 * Kiểu PHẢI khớp `RuntimeTrace.pending_approvals` (src/aoip/agent/trace.py:135) —
 * hàm đó trả `list[dict]`, không phải `list[str]`. Bản trước khai `string[]` rồi
 * render thẳng phần tử làm React child; khi hàng đợi có dữ liệu thật, React ném
 * "Objects are not valid as a React child" và cả trang trắng. Lỗi KHÔNG lộ ra khi
 * hàng đợi rỗng — đó là lý do nó sống sót tới tận đây.
 */
interface PendingApproval {
  reason: string;
  timestamp: number;
  correlation_id: string;
  tenant_id: string;
  incident_id?: string | null;
  decision_id?: string | null;
  action_id?: string | null;
  command_id?: string | null;
  agent_id?: string | null;
  mission_id?: string | null;
  canonical_scope?: string | null;
}

function formatTime(ts: number): string {
  if (!Number.isFinite(ts) || ts <= 0) return "—";
  // timestamp là epoch GIÂY (time.time() phía Python); Date cần mili-giây.
  return new Date(ts * 1000).toLocaleString("vi-VN");
}

export default async function TenantApprovalsPage() {
  const resp = await backendGet(backendConfig, "/approvals", (await headers()).get("cookie") ?? "");
  if (!resp.ok) {
    return (
      <Card error>
        <div className="aoip-state">Không tải được hàng đợi phê duyệt ({resp.status}).</div>
      </Card>
    );
  }

  const body = (await resp.json()) as { tenant: string; pending: PendingApproval[] };
  // Phòng thủ: backend đổi hình dạng thì hiện trạng thái rỗng, không làm trắng trang.
  const pending = Array.isArray(body.pending) ? body.pending : [];

  return (
    <>
      <h1>Phê duyệt</h1>
      <p className="aoip-muted">Các quyết định cần người có thẩm quyền xác nhận.</p>
      <Card>
        {pending.length === 0 ? (
          <div className="aoip-state">Không có phê duyệt đang chờ.</div>
        ) : (
          <ul>
            {pending.map((item) => (
              <li key={item.correlation_id}>
                <div>{item.reason || "Không có lý do được ghi"}</div>
                <div className="aoip-muted">
                  <code>{item.correlation_id}</code>
                  {item.incident_id ? (
                    <>
                      {" · sự cố "}
                      <code>{item.incident_id}</code>
                    </>
                  ) : null}
                  {item.agent_id ? (
                    <>
                      {" · agent "}
                      <code>{item.agent_id}</code>
                    </>
                  ) : null}
                  {" · "}
                  {formatTime(item.timestamp)}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}
