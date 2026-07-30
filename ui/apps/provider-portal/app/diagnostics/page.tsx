import Link from "next/link";
import { Card } from "@aoip/ui-kit";
import { PageIntro } from "@/components/PageIntro";
import {
  fetchRecentTraces,
  isDiagnosticLane,
  scopeVI,
  STAGE_VI,
  type RecentTrace,
} from "@/lib/pipeline";
import { TestPanel } from "./TestPanel";
import "./diagnostics.css";

function timeVI(ts: number): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("vi-VN", { hour12: false });
}

// Kết luận thô → nhãn + màu. Suy ra "không phát báo động" (Lô B) từ verdict skip.
function verdictBadge(verdict: string): { cls: string; label: string } {
  const v = (verdict || "").toLowerCase();
  if (!v || v.includes("đang")) return { cls: "wait", label: "đang xử lý" };
  if (v.includes("no real") || v.includes("observed") || v.includes("not alarmed")) {
    return { cls: "muted", label: "không có sự cố thực chất" };
  }
  if (v.includes("diagnosis") || v.includes("emitted")) return { cls: "ok", label: "đã chẩn đoán" };
  return { cls: "ok", label: verdict.slice(0, 40) };
}

function Intro() {
  return (
    <PageIntro
      title="Chẩn đoán & Test"
      lead="Xem Omni đã chẩn đoán từng sự cố ra sao — từng bước một — và tự đẩy một sự cố mẫu để kiểm chứng ngay."
    />
  );
}

export default async function DiagnosticsPage() {
  const result = await fetchRecentTraces();
  const traces: RecentTrace[] = result.data?.traces ?? [];
  const incidents = traces.filter((t) => isDiagnosticLane(t.lane));

  return (
    <>
      <Intro />

      <div className="aoip-k">Tự kiểm chứng — đẩy một sự cố mẫu</div>
      <Card>
        <TestPanel />
      </Card>

      <div className="aoip-k">Các lượt chẩn đoán gần đây</div>
      {result.error && !result.data ? (
        <Card error>
          <div className="aoip-state" data-testid="diagnostics-error">
            Không tải được danh sách ({result.error}). Thử tải lại trang.
          </div>
        </Card>
      ) : incidents.length === 0 ? (
        <Card>
          <div className="aoip-state" data-testid="diagnostics-empty">
            Chưa có lượt chẩn đoán nào trong 1 giờ gần đây. Bấm một nút &quot;Test lại&quot;
            phía trên để tạo một lượt và xem Omni xử lý từng bước.
          </div>
        </Card>
      ) : (
        <Card>
          <table className="aoip-diag-table" data-testid="diagnostics-table">
            <thead>
              <tr>
                <th>Thời điểm</th>
                <th>Lĩnh vực</th>
                <th>Bước hiện tại</th>
                <th>Kết luận</th>
                <th>Chi tiết từng bước</th>
              </tr>
            </thead>
            <tbody>
              {incidents.map((t) => {
                const b = verdictBadge(t.verdict);
                return (
                  <tr key={t.trace_id}>
                    <td>{timeVI(t.updated_at)}</td>
                    <td>{scopeVI(t.domain ?? t.lane)}</td>
                    <td>{STAGE_VI[t.current_stage]?.name ?? t.current_stage ?? "—"}</td>
                    <td>
                      <span className={`aoip-diag-badge ${b.cls}`}>{b.label}</span>
                    </td>
                    <td>
                      <Link
                        href={`/pipeline/${encodeURIComponent(t.trace_id)}`}
                        className="aoip-trace-link"
                        data-testid="diag-detail-link"
                      >
                        Xem từng bước →
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}

      <p className="aoip-muted aoip-diag-foot">
        Mỗi lượt mở ra đủ chuỗi: bằng chứng thu được → tra kiến thức (RAG) → lập luận của
        LLM qua từng lượt, kèm lệnh read-only đã chạy và kết quả → ghi CRAT → phát cảnh báo.
        Lượt kết luận &quot;không có sự cố thực chất&quot; cố ý KHÔNG phát cảnh báo đỏ nhưng
        vẫn lưu đầy đủ ở đây để kiểm.
      </p>
    </>
  );
}
