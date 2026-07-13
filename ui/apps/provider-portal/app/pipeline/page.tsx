import Link from "next/link";
import { Card } from "@aoip/ui-kit";
import { PageIntro } from "@/components/PageIntro";
import { fetchRecentTraces, LANE_VI, STAGE_VI } from "@/lib/pipeline";
import "./pipeline.css";

function timeVI(ts: number): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("vi-VN", { hour12: false });
}

export default async function PipelinePage() {
  const result = await fetchRecentTraces();

  return (
    <>
      <PageIntro
        title="Xử lý sự cố (Pipeline)"
        lead="Mỗi khi hệ thống của khách hàng có dấu hiệu bất thường, Omni xử lý qua 12 bước cố định — từ tiếp nhận tín hiệu đến kiểm tra sau khi khắc phục. Trang này liệt kê các lượt xử lý gần nhất; bấm vào từng lượt để xem hệ thống đã làm gì ở mỗi bước."
        terms={[
          { term: "Lượt xử lý (trace)", meaning: "Một lần hệ thống tiếp nhận và xử lý trọn vẹn một dấu hiệu bất thường." },
          { term: "Làn (lane)", meaning: "Loại vấn đề: tài nguyên máy chủ, hỏng dịch vụ, lỗi ứng dụng web, hay an ninh." },
          { term: "Kết luận (verdict)", meaning: "Quyết định cuối của hệ thống: chỉ khuyến nghị, tự thực thi, hay chờ người duyệt." },
        ]}
      />

      {result.error || !result.data ? (
        <Card error>
          <div className="aoip-k err">Không tải được danh sách lượt xử lý</div>
          <div className="aoip-state" data-testid="pipeline-error">
            Nguồn dữ liệu ({result.error ?? "không phản hồi"}). Thử tải lại trang.
          </div>
        </Card>
      ) : result.data.traces.length === 0 ? (
        <Card>
          <div className="aoip-state" data-testid="pipeline-empty">
            Chưa có lượt xử lý nào gần đây — nghĩa là các hệ thống đang được giám sát không có
            dấu hiệu bất thường. Đây là trạng thái tốt.
          </div>
        </Card>
      ) : (
        <Card>
          <table className="aoip-pipeline-table" data-testid="pipeline-table">
            <thead>
              <tr>
                <th>Thời điểm</th>
                <th>Loại vấn đề</th>
                <th>Đang ở bước</th>
                <th>Kết luận</th>
                <th>Mã lượt xử lý</th>
              </tr>
            </thead>
            <tbody>
              {result.data.traces.map((t) => (
                <tr key={t.trace_id}>
                  <td>{timeVI(t.updated_at)}</td>
                  <td>{LANE_VI[t.lane] ?? t.lane ?? "—"}</td>
                  <td>{STAGE_VI[t.current_stage]?.name ?? t.current_stage ?? "—"}</td>
                  <td>{t.verdict || "đang xử lý"}</td>
                  <td>
                    <Link href={`/pipeline/${encodeURIComponent(t.trace_id)}`} className="aoip-trace-link">
                      {t.trace_id.slice(0, 20)}…
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </>
  );
}
