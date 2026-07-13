import { Card, MetricStat } from "@aoip/ui-kit";
import { PageIntro } from "@/components/PageIntro";
import { fetchKpiSummary, fetchKpiTrend, percentVI } from "@/lib/kpi";
import { LANE_VI } from "@/lib/pipeline";

export default async function KpiPage() {
  const [summary, trend] = await Promise.all([fetchKpiSummary(), fetchKpiTrend()]);

  return (
    <>
      <PageIntro
        title="Số liệu 24 giờ qua"
        lead="Trang này trả lời câu hỏi: trong 24 giờ qua hệ thống đã phát hiện bao nhiêu vấn đề, xử lý được bao nhiêu, và chất lượng chẩn đoán ra sao. Số 0 ở mọi ô nghĩa là không có sự cố nào — trạng thái tốt, không phải lỗi hiển thị."
        terms={[
          { term: "Khuyến nghị được chấp nhận", meaning: "Chẩn đoán của hệ thống được xác nhận đúng (bởi người duyệt hoặc kết quả kiểm chứng)." },
          { term: "Báo động nhầm (false positive)", meaning: "Hệ thống tưởng có vấn đề nhưng thực tế không có — càng thấp càng tốt." },
          { term: "Phát hiện / Đã xử lý", meaning: "Số vấn đề nhìn thấy và số vấn đề đã khắc phục xong, chia theo loại." },
        ]}
      />

      {summary.error || !summary.data ? (
        <Card error>
          <div className="aoip-k err">Không tải được số liệu tổng hợp</div>
          <div className="aoip-state" data-testid="kpi-error">
            Nguồn dữ liệu ({summary.error ?? "không phản hồi"}). Thử tải lại trang.
          </div>
        </Card>
      ) : (
        <>
          <div className="aoip-k">Chất lượng chẩn đoán</div>
          <div className="aoip-grid" data-testid="kpi-summary">
            <MetricStat
              label="Khuyến nghị đưa ra"
              value={summary.data.advisory.total}
              hint="Tổng số lần hệ thống đưa ra chẩn đoán + đề xuất khắc phục"
            />
            <MetricStat
              label="Được chấp nhận"
              value={summary.data.advisory.accepted}
              hint="Chẩn đoán được xác nhận là đúng"
            />
            <MetricStat
              label="Tỷ lệ chính xác"
              value={percentVI(summary.data.advisory.acceptance_rate)}
              hint="Phần trăm khuyến nghị được chấp nhận"
            />
            <MetricStat
              label="Báo động nhầm"
              value={summary.data.execution.false_positive}
              hint="Số lần cảnh báo nhưng thực tế không có vấn đề — càng thấp càng tốt"
            />
          </div>
        </>
      )}

      {trend.data && (
        <Card>
          <div className="aoip-k">Phát hiện & xử lý theo loại vấn đề (24h)</div>
          <div className="aoip-grid" data-testid="kpi-trend">
            {Object.entries(trend.data.lanes).map(([lane, v]) => (
              <MetricStat
                key={lane}
                label={LANE_VI[lane] ?? lane}
                value={`${v.detected} phát hiện · ${v.resolved} đã xử lý`}
              />
            ))}
          </div>
        </Card>
      )}
    </>
  );
}
