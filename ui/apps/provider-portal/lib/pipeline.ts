// Pipeline xử lý sự cố — chiếu omni:trace:stages qua gateway /trace/* (read-only).
// Nguồn: src/gateway/routes/trace.py + src/pkg/observability/pipeline_stages.py.
import { fetchGatewaySection, type GatewaySectionResult } from "@/lib/gateway";

export interface RecentTrace {
  trace_id: string;
  /** Lĩnh vực kỹ thuật — một trong 9 domain canonical. Rỗng ở trace cũ (TTL 1h). */
  domain: string;
  /** `diagnostic` = sự cố · `learning` = tín hiệu học hỏi. Trục ĐỘC LẬP với domain. */
  signal_kind: string;
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
  domain: string;
  signal_kind: string;
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

// 9 lĩnh vực kỹ thuật (domain) — trục phân loại sự cố DUY NHẤT. 4 "lane" cũ đã gỡ
// hẳn khỏi tầng trace (2026-08-09). Vì sao bỏ: lane là thuộc tính của một CẢNH BÁO,
// và 4 lane không diễn đạt được
// mạng/đĩa/cơ sở dữ liệu/phần cứng — nên sự cố thuộc các lĩnh vực đó không gọi được
// đúng bộ chẩn đoán. Nguồn sự thật: `src/pkg/domain/taxonomy.py`.
export const DOMAIN_VI: Record<string, string> = {
  os_host: "Máy chủ / hệ điều hành (CPU, RAM, tải)",
  kubernetes: "Kubernetes (pod, container, cụm)",
  network: "Mạng (kết nối, cổng, định tuyến)",
  storage: "Lưu trữ (đĩa, phân vùng, inode)",
  database: "Cơ sở dữ liệu (MySQL, Postgres, Redis)",
  service: "Dịch vụ hệ thống (systemd, tiến trình nền)",
  application: "Ứng dụng (lỗi 5xx, quá tải, log lỗi)",
  security: "An ninh — dấu hiệu tấn công",
  hardware: "Phần cứng (nhiệt độ, ổ đĩa, quạt)",
  // `unknown` KHÔNG bị ẩn: dữ liệu lịch sử từng gánh tới bốn
  // lĩnh vực nên không suy ra được một lĩnh vực cụ thể. Ẩn đi là làm hụt số thật.
  unknown: "Chưa phân loại được lĩnh vực",
};

/** Nhãn tiếng Việt cho một trong 9 lĩnh vực canonical. */
export function scopeVI(scope: string): string {
  if (!scope) return "Không rõ lĩnh vực";
  return DOMAIN_VI[scope] ?? DOMAIN_VI[scope.toLowerCase()] ?? scope;
}

// Tín hiệu KHÔNG phải sự cố (INV_KNOWLEDGE_NOT_ALERT): discovery/knowledge chỉ chạm
// đúng 1 bước EVIDENCE rồi rẽ vào knowledge pipeline — KHÔNG BAO GIỜ đi 12 bước chẩn
// đoán. Hiển thị chúng như sự cố "đang xử lý" là sai bản chất (đã gây hiểu nhầm
// "đứng hàng loạt" — 2026-07-13).
//
// Nhận cả hai từ vựng vì hai thế hệ dữ liệu cùng tồn tại. `unknown` KHÔNG được coi là
// tín hiệu học hỏi: một sự cố lịch sử `SYS_HARD_FAIL` chuẩn hoá thành `unknown` vẫn là
// sự cố thật — xếp nó sang nhóm "học hỏi" là làm biến mất sự cố khỏi danh sách.
/** Sự cố hay tín hiệu học hỏi? Đọc `signal_kind` — KHÔNG suy từ lĩnh vực.
 *
 * Trước 2026-08-09 hàm này nhận `lane` và so với bảng lane cũ; `lane` khi đó gánh
 * cả bốn nghĩa (lane trục A, proof_lane, loại tín hiệu, rỗng) nên phân loại sai.
 * Trace cũ không có `signal_kind` ⇒ suy tạm từ `domain` để không mất hiển thị
 * trong 1 giờ TTL còn lại của chúng.
 */
export function isDiagnosticSignal(t: { signal_kind?: string; domain?: string }): boolean {
  if (t.signal_kind) return t.signal_kind === "diagnostic";
  return Boolean(t.domain && t.domain.toLowerCase() in DOMAIN_VI);
}

export function learningSignalVI(): string {
  return "Khám phá hệ thống (agent rà quét định kỳ)";
}

export function stageStatusVI(status: string): string {
  switch (status) {
    case "ok": return "Xong";
    case "fail": return "Lỗi";
    case "skip": return "Bỏ qua (không cần)";
    default: return "Chưa tới";
  }
}
