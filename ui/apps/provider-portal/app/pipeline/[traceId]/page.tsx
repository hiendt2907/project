import Link from "next/link";
import { Card, KeyVal } from "@aoip/ui-kit";
import { PageIntro } from "@/components/PageIntro";
import {
  fetchTracePipeline,
  isDiagnosticLane,
  LANE_VI,
  learningLaneVI,
  STAGE_VI,
  stageStatusVI,
} from "@/lib/pipeline";
import "../pipeline.css";

function timeVI(ts: number): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("vi-VN", { hour12: false });
}

export default async function TraceDetailPage({
  params,
}: {
  params: Promise<{ traceId: string }>;
}) {
  const { traceId } = await params;
  const result = await fetchTracePipeline(traceId);

  return (
    <>
      <PageIntro
        title="Chi tiết một lượt xử lý"
        lead="Dưới đây là 12 bước hệ thống đi qua cho lượt xử lý này, theo đúng thứ tự thời gian. Chấm xanh = bước đã hoàn thành, đỏ = bước gặp lỗi, xám = bước được bỏ qua vì không cần thiết, vòng đứt nét = chưa tới. Mỗi bước có giải thích bằng lời để bạn biết hệ thống đang làm gì."
      />

      <p>
        <Link href="/pipeline" className="aoip-trace-link">← Quay lại danh sách lượt xử lý</Link>
      </p>

      {result.error || !result.data ? (
        <Card error>
          <div className="aoip-k err">Không tải được lượt xử lý này</div>
          <div className="aoip-state" data-testid="trace-error">
            {result.error?.includes("404")
              ? "Lượt xử lý không còn trong bộ nhớ (dữ liệu chi tiết chỉ giữ 1 giờ)."
              : `Nguồn dữ liệu (${result.error ?? "không phản hồi"}). Thử tải lại trang.`}
          </div>
        </Card>
      ) : !isDiagnosticLane(result.data.lane) ? (
        // Tín hiệu học hỏi (INV_KNOWLEDGE_NOT_ALERT): theo thiết kế chỉ có 1 bước —
        // KHÔNG vẽ 12 bước chẩn đoán, tránh cảm giác "kẹt ở bước 2" sai bản chất.
        <Card>
          <KeyVal label="Mã lượt xử lý">{result.data.trace_id}</KeyVal>
          <KeyVal label="Loại tín hiệu">{learningLaneVI(result.data.lane)}</KeyVal>
          <KeyVal label="Thời điểm">{timeVI(result.data.updated_at)}</KeyVal>
          <KeyVal label="Trạng thái" testid="learning-status">
            ✓ Đã ghi nhận vào kho hiểu biết — hoàn thành
          </KeyVal>
          <div className="aoip-muted" data-testid="learning-explain">
            Đây không phải sự cố. Agent gửi bản rà quét định kỳ để Omni hiểu hệ thống sâu
            hơn; loại tín hiệu này theo thiết kế chỉ có MỘT bước (ghi nhận bằng chứng) và
            không đi qua 12 bước chẩn đoán — không có gì đang chờ xử lý ở đây.
            {result.data.stages.find((s) => s.stage === "EVIDENCE")?.detail && (
              <> Chi tiết ghi nhận: <code>{result.data.stages.find((s) => s.stage === "EVIDENCE")?.detail}</code></>
            )}
          </div>
        </Card>
      ) : (
        <>
          <Card>
            <KeyVal label="Mã lượt xử lý">{result.data.trace_id}</KeyVal>
            <KeyVal label="Loại vấn đề">{LANE_VI[result.data.lane] ?? result.data.lane ?? "—"}</KeyVal>
            <KeyVal label="Bắt đầu">{timeVI(result.data.started_at)}</KeyVal>
            <KeyVal label="Cập nhật gần nhất">{timeVI(result.data.updated_at)}</KeyVal>
            <KeyVal label="Kết luận">{result.data.verdict || "đang xử lý"}</KeyVal>
          </Card>

          <Card>
            <ol className="aoip-stage-list" data-testid="stage-list">
              {result.data.stages.map((s) => {
                const vi = STAGE_VI[s.stage];
                return (
                  <li key={s.stage} className="aoip-stage">
                    <span className="aoip-stage-light" data-status={s.status} aria-label={stageStatusVI(s.status)} />
                    <span className="aoip-stage-name">
                      {vi?.name ?? s.stage}
                      <small>{s.stage}</small>
                    </span>
                    <span>
                      <span className="aoip-stage-explain">{vi?.explain ?? ""}</span>
                      {s.detail && <div className="aoip-stage-detail">{s.detail}</div>}
                    </span>
                    <span className="aoip-stage-status" data-status={s.status}>
                      {stageStatusVI(s.status)}
                      {s.elapsed_ms > 0 && ` · ${(s.elapsed_ms / 1000).toFixed(1)}s`}
                    </span>
                  </li>
                );
              })}
            </ol>
          </Card>
        </>
      )}
    </>
  );
}
