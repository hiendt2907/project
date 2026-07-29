/**
 * Kiểu + cách diễn đạt cho hai trang "Năng lực" và "Đơn xin quyền".
 *
 * Kiểu PHẢI khớp hình dạng backend thật:
 *  - `CompetencyPattern`  ≈ `CompetencyReport.as_dict()` (src/services/case_ledger/scoring.py)
 *    cộng ba trường ghép từ `scope_grant` ở tầng route.
 *  - `ScopeRequest`       ≈ một dòng `omni_admin.scope_request`
 *    (migrations/omni_admin/0012_case_ledger.sql), `evidence` đã được parse
 *    thành object chứ KHÔNG còn là chuỗi JSON.
 *
 * Bài học từ `app/approvals/page.tsx`: khai sai kiểu rồi render object làm React
 * child sẽ làm TRẮNG trang, và chỉ lộ ra khi có dữ liệu thật. Vì vậy mọi trường
 * dùng để render ở đây đều đi qua helper phòng thủ bên dưới.
 */

export interface CompetencyPattern {
  pattern_key: string;
  tenant_id: string;
  total_cases: number;
  diagnosed: number;
  refused: number;
  out_of_scope: number;
  correct: number;
  incorrect: number;
  partial: number;
  unjudged: number;
  accuracy_lower_bound: number;
  accuracy_raw: number;
  coverage: number;
  unjudged_ratio: number;
  recurrence_rate: number;
  eligible: boolean;
  blockers: string[];
  granted_scope: string;
  frozen: boolean;
  frozen_reason: string | null;
}

export interface ScopeRequest {
  id: number;
  tenant_id: string;
  pattern_key: string;
  requested_scope: string;
  /** Bản đóng băng của `CompetencyReport.as_dict()` lúc nộp đơn. */
  evidence: Partial<CompetencyPattern> | null;
  state: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  cooldown_until: string | null;
  crat_ref: string | null;
  created_at: string | null;
}

/** Ngưỡng phía backend (scoring.py) — hiện ra để admin biết đang so với cái gì. */
export const NGUONG = {
  doTinCayToiThieu: 0.7,
  doPhu: 0.5,
  chuaChamToiDa: 0.4,
} as const;

/** Bậc quyền, gọi bằng tiếng người. Thang đi lên từng bậc một. */
export const SCOPE_LABEL: Record<string, string> = {
  SUGGEST_ONLY: "Chỉ được đề xuất",
  HITL_REQUIRED: "Làm khi người duyệt",
  AUTO_EXECUTE: "Tự làm",
};

export const STATE_LABEL: Record<string, string> = {
  PENDING: "Đang chờ bạn",
  APPROVED: "Đã đồng ý",
  REJECTED: "Đã từ chối",
  WITHDRAWN: "Đã rút",
};

export function scopeLabel(value: unknown): string {
  const key = typeof value === "string" ? value : "";
  return SCOPE_LABEL[key] ?? (key || "—");
}

export function stateLabel(value: unknown): string {
  const key = typeof value === "string" ? value : "";
  return STATE_LABEL[key] ?? (key || "—");
}

/** 0.4385 → "44%". Số không hợp lệ trả "—" thay vì "NaN%". */
export function phanTram(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Math.round(value * 100)}%`;
}

export function soNguyen(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

export function ngayGio(value: unknown): string {
  if (typeof value !== "string" || !value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString("vi-VN");
}

/**
 * Blocker backend viết cho kỹ sư ("cận dưới độ chính xác 0.44 < 0.70"). Admin hệ
 * thống của khách không đọc thứ đó. Dịch sang câu nói thường, giữ nguyên con số
 * để vẫn đối chiếu được với sổ ca; chuỗi lạ thì trả nguyên văn chứ không nuốt.
 */
export function blockerDeHieu(raw: unknown): string {
  if (typeof raw !== "string" || !raw.trim()) return "Chưa rõ lý do";
  const s = raw.trim();

  let m = /cận dưới độ chính xác ([\d.]+) < ([\d.]+)/.exec(s);
  if (m) {
    return `Độ tin cậy tối thiểu mới ${phanTram(Number(m[1]))}, cần từ ${phanTram(
      Number(m[2]),
    )} trở lên — chưa đủ số ca đúng để chắc chắn.`;
  }

  m = /độ phủ ([\d.]+) < ([\d.]+)/.exec(s);
  if (m) {
    return `Độ phủ mới ${phanTram(Number(m[1]))}, cần từ ${phanTram(
      Number(m[2]),
    )} trở lên — Omni đang từ chối quá nhiều ca.`;
  }

  m = /tỉ lệ chưa phán quyết ([\d.]+) > ([\d.]+)/.exec(s);
  if (m) {
    return `${phanTram(Number(m[1]))} số ca chưa ai chấm đúng/sai (tối đa cho phép ${phanTram(
      Number(m[2]),
    )}) — cần người xác nhận kết quả thì mới tính được.`;
  }

  if (s.includes("chưa có ca nào được chẩn đoán")) {
    return "Chưa có ca nào Omni đưa ra kết luận, nên chưa có gì để đánh giá.";
  }
  if (s.includes("chưa có ca nào được phán quyết")) {
    return "Có kết luận nhưng chưa ai chấm đúng/sai, nên chưa tính được độ chính xác.";
  }

  m = /(\d+) ca có posture không hợp lệ/.exec(s);
  if (m) {
    return `${m[1]} ca ghi nhận bị hỏng dữ liệu — cần kiểm tra lại sổ ca.`;
  }

  return s;
}
