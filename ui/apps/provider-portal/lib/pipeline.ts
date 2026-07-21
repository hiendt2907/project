// Pipeline xử lý sự cố — chiếu omni:trace:stages qua gateway /trace/* (read-only).
// Nguồn: src/gateway/routes/trace.py + src/pkg/observability/pipeline_stages.py.
import { fetchGatewaySection, type GatewaySectionResult } from "@/lib/gateway";

export interface RecentTrace {
  trace_id: string;
  lane: string;
  current_stage: string;
  verdict: string;
  started_at: number;
  updated_at: number;
}

export interface PipelineStage {
  stage: string;
  status: "ok" | "fail" | "skip" | "pending";
  ts: number;
  detail: string;
  elapsed_ms: number;
}

export interface TracePipeline {
  found: boolean;
  trace_id: string;
  lane: string;
  started_at: number;
  updated_at: number;
  verdict: string;
  stages: PipelineStage[];
}

export async function fetchRecentTraces(): Promise<GatewaySectionResult<{ traces: RecentTrace[] }>> {
  return fetchGatewaySection("/trace/recent");
}

export async function fetchTracePipeline(traceId: string): Promise<GatewaySectionResult<TracePipeline>> {
  return fetchGatewaySection(`/trace/${encodeURIComponent(traceId)}/pipeline`);
}

// ── Bản dịch đời thường (nhất quán toàn portal) ─────────────────────────────
// 13 bước phải khớp PIPELINE_STAGES (src/pkg/observability/pipeline_stages.py).
export const STAGE_VI: Record<string, { name: string; explain: string }> = {
  INGEST: { name: "Tiếp nhận", explain: "Hệ thống nhận tín hiệu bất thường từ nơi giám sát." },
  EVIDENCE: { name: "Thu thập bằng chứng", explain: "Gom số liệu, log và trạng thái liên quan để có bức tranh đầy đủ." },
  RAG: { name: "Tra cứu kinh nghiệm", explain: "Đối chiếu với kho kiến thức các sự cố đã gặp trước đây." },
  LLM: { name: "AI phân tích", explain: "AI đọc toàn bộ bằng chứng và đưa ra chẩn đoán nguyên nhân." },
  VERIFY: { name: "Kiểm chứng", explain: "Kiểm tra lại chẩn đoán bằng phép thử thật, không tin lời AI suông." },
  SCHEMA: { name: "Chuẩn hoá kết luận", explain: "Ép kết luận vào mẫu chuẩn (cái gì hỏng, ở đâu, vì sao, xử lý thế nào)." },
  KILLSWITCH: { name: "Công tắc an toàn", explain: "Kiểm tra cấu hình an toàn: có được phép tự động sửa hay không." },
  CRAT: { name: "Ghi sổ kiểm toán", explain: "Ghi quyết định vào sổ kiểm toán chống sửa đổi TRƯỚC khi làm bất cứ gì." },
  DISPATCH: { name: "Gửi khuyến nghị", explain: "Gửi kết luận và đề xuất khắc phục tới kênh thông báo (Telegram)." },
  HITL: { name: "Chờ người duyệt", explain: "Việc nhạy cảm phải có con người bấm duyệt mới được làm tiếp." },
  EXECUTOR: { name: "Thực thi", explain: "Thực hiện thao tác khắc phục đã được phép." },
  FEEDBACK: { name: "Đánh giá lại", explain: "Kiểm tra sau khi sửa: sự cố đã hết chưa, có cần làm thêm không." },
  AUTO_RECOVERY: { name: "Tự động khắc phục", explain: "AI tự đề xuất khắc phục và gửi lệnh qua kênh đã được phê duyệt (cấp bậc tự động + ngưỡng tin cậy)." },
};

export const LANE_VI: Record<string, string> = {
  SYS_RESOURCE: "Tài nguyên máy chủ (CPU/RAM bất thường)",
  SYS_HARD_FAIL: "Hỏng hóc hệ điều hành / dịch vụ",
  APP_HTTP: "Lỗi ứng dụng web (5xx, quá tải)",
  SIEM_SECURITY: "An ninh — dấu hiệu tấn công",
};

// Tín hiệu KHÔNG phải sự cố (INV_KNOWLEDGE_NOT_ALERT): discovery/knowledge chỉ chạm
// đúng 1 bước EVIDENCE rồi rẽ vào knowledge pipeline — KHÔNG BAO GIỜ đi 12 bước chẩn
// đoán. Hiển thị chúng như sự cố "đang xử lý" là sai bản chất (đã gây hiểu nhầm
// "đứng hàng loạt" — 2026-07-13). Chỉ 4 lane trong LANE_VI mới là pipeline chẩn đoán.
export function isDiagnosticLane(lane: string): boolean {
  return lane in LANE_VI;
}

export const LEARNING_LANE_VI: Record<string, string> = {
  ONBOARDING_DISCOVERY: "Khám phá hệ thống (agent rà quét định kỳ)",
};

export function learningLaneVI(lane: string): string {
  return LEARNING_LANE_VI[lane] ?? `Tín hiệu học hỏi (${lane || "không rõ loại"})`;
}

export function stageStatusVI(status: string): string {
  switch (status) {
    case "ok": return "Xong";
    case "fail": return "Lỗi";
    case "skip": return "Bỏ qua (không cần)";
    default: return "Chưa tới";
  }
}
