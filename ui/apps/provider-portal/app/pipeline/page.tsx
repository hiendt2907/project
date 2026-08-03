import Link from "next/link";
import { Card } from "@aoip/ui-kit";
import { PageIntro } from "@/components/PageIntro";
import {
  fetchRecentTraces,
  isDiagnosticLane,
  scopeVI,
  learningLaneVI,
  STAGE_VI,
  type RecentTrace,
} from "@/lib/pipeline";
import "./pipeline.css";

function timeVI(ts: number): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("vi-VN", { hour12: false });
}

export default async function PipelinePage() {
  const result = await fetchRecentTraces();

  if (result.error || !result.data) {
    return (
      <>
        <Intro />
        <Card error>
          <div className="aoip-k err">Không tải được danh sách lượt xử lý</div>
          <div className="aoip-state" data-testid="pipeline-error">
            Nguồn dữ liệu ({result.error ?? "không phản hồi"}). Thử tải lại trang.
          </div>
        </Card>
      </>
    );
  }

  // Tách theo bản chất: sự cố (12 bước chẩn đoán) vs tín hiệu học hỏi (1 bước,
  // xong là hoàn thành — không phải "đang xử lý", không phải kẹt).
  const incidents = result.data.traces.filter((t) => isDiagnosticLane(t.lane));
  const learning = result.data.traces.filter((t) => !isDiagnosticLane(t.lane));

  return (
    <>
      <Intro />

      <div className="aoip-k">Sự cố đang/đã xử lý (pipeline 12 bước)</div>
      {incidents.length === 0 ? (
        <Card>
          <div className="aoip-state" data-testid="pipeline-empty">
            Không có sự cố nào trong 1 giờ gần đây — các hệ thống đang được giám sát không có
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
              {incidents.map((t) => (
                <tr key={t.trace_id}>
                  <td>{timeVI(t.updated_at)}</td>
                  <td>{scopeVI(t.domain ?? t.lane)}</td>
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

      <div className="aoip-k">Tín hiệu học hỏi (không phải sự cố)</div>
      <Card>
        <p className="aoip-muted aoip-learning-note">
          Agent trên máy khách hàng gửi về đều đặn các bản rà quét (dịch vụ, tài liệu, cấu
          hình) để Omni hiểu hệ thống sâu hơn. Loại tín hiệu này theo thiết kế chỉ có MỘT
          bước — ghi nhận vào kho hiểu biết — nên &quot;đã ghi nhận ✓&quot; nghĩa là hoàn
          thành, không phải đang chờ xử lý.
        </p>
        {learning.length === 0 ? (
          <div className="aoip-state" data-testid="learning-empty">
            Không có tín hiệu học hỏi nào trong 1 giờ gần đây.
          </div>
        ) : (
          <ul className="aoip-learning-list" data-testid="learning-list">
            {learning.map((t) => (
              <LearningRow key={t.trace_id} trace={t} />
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

function LearningRow({ trace }: { trace: RecentTrace }) {
  return (
    <li className="aoip-learning-row">
      <span className="aoip-learning-check" aria-hidden>✓</span>
      <span>
        {learningLaneVI(trace.lane)} — đã ghi nhận vào kho hiểu biết
        <span className="aoip-muted"> · {timeVI(trace.updated_at)}</span>
      </span>
      <Link
        href={`/pipeline/${encodeURIComponent(trace.trace_id)}`}
        className="aoip-trace-link aoip-learning-link"
      >
        {trace.trace_id.slice(0, 20)}…
      </Link>
    </li>
  );
}

function Intro() {
  return (
    <PageIntro
      title="Xử lý sự cố (Pipeline)"
      lead="Khi hệ thống của khách hàng có dấu hiệu bất thường, Omni xử lý qua 12 bước cố định — danh sách «Sự cố» bên dưới. Ngoài ra agent còn gửi về các bản rà quét định kỳ để Omni học hệ thống; chúng được liệt kê riêng ở khu «Tín hiệu học hỏi» vì không phải sự cố và hoàn thành ngay sau một bước."
      terms={[
        { term: "Lượt xử lý (trace)", meaning: "Một lần hệ thống tiếp nhận và xử lý trọn vẹn một tín hiệu." },
        { term: "Sự cố", meaning: "Tín hiệu bất thường thật sự — đi đủ 12 bước chẩn đoán và khắc phục." },
        { term: "Tín hiệu học hỏi", meaning: "Bản rà quét định kỳ giúp Omni hiểu hệ thống — chỉ có 1 bước ghi nhận, xong ngay, không cần ai xử lý." },
        { term: "Kết luận (verdict)", meaning: "Quyết định cuối cho sự cố: chỉ khuyến nghị, tự thực thi, hay chờ người duyệt." },
      ]}
    />
  );
}
