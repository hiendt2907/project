import { headers } from "next/headers";
import { backendGet } from "@aoip/api-client";
import { Card } from "@aoip/ui-kit";
import { backendConfig } from "@/lib/config";
import {
  blockerDeHieu,
  ngayGio,
  NGUONG,
  phanTram,
  scopeLabel,
  soNguyen,
  type CompetencyPattern,
} from "@/lib/competency";
import { MetricPair } from "../MetricPair";
import styles from "../ledger.module.css";

/**
 * "Năng lực" — Omni đã làm được gì, theo TỪNG loại việc.
 *
 * Người đọc là admin hệ thống của khách, không phải kỹ sư ML: không có chữ
 * "Wilson", "lower bound", "confidence interval" nào trên trang này. Con số vẫn
 * là con số backend tính ra, chỉ đổi cách gọi.
 */
export default async function TenantCompetencyPage() {
  const resp = await backendGet(
    backendConfig,
    "/competency/patterns",
    (await headers()).get("cookie") ?? "",
  );
  if (!resp.ok) {
    return (
      <>
        <h1>Năng lực</h1>
        <Card error>
          <div className="aoip-state">
            Không tải được hồ sơ năng lực ({resp.status}). Thử lại sau ít phút.
          </div>
        </Card>
      </>
    );
  }

  const body = (await resp.json()) as { tenant_id?: string; patterns?: CompetencyPattern[] };
  // Phòng thủ: backend đổi hình dạng thì hiện trạng thái rỗng, không làm trắng trang.
  const patterns = Array.isArray(body.patterns) ? body.patterns : [];
  const duDieuKien = patterns.filter((p) => p?.eligible).length;

  return (
    <>
      <h1>Năng lực</h1>
      <p className="aoip-muted">
        Omni làm được đến đâu với từng loại việc, tính từ những ca có thật đã ghi sổ.
        Không có con số nào ở đây do Omni tự chấm cho mình.
      </p>

      <p className={styles.note}>
        <strong>Đọc hai cột cạnh nhau, đừng đọc một cột.</strong> Làm đúng 3/3 ca nghe
        như hoàn hảo, nhưng ba lần thì chưa nói lên điều gì — nên chúng tôi lấy{" "}
        <em>độ tin cậy tối thiểu</em>: mức thấp nhất mà kết quả vẫn còn đứng vững nếu
        gặp thêm ca mới (3/3 chỉ ra khoảng {phanTram(0.4385)}). Còn{" "}
        <em>độ phủ</em> là tỉ lệ ca Omni dám nhận. Nếu chỉ nhìn độ chính xác, cách dễ
        nhất để đạt điểm cao là từ chối hết ca khó — trông rất cẩn thận mà chẳng giúp
        được gì. Đủ điều kiện xin quyền cần cả hai: tin cậy tối thiểu từ{" "}
        {phanTram(NGUONG.doTinCayToiThieu)} và độ phủ từ {phanTram(NGUONG.doPhu)}.
      </p>

      <Card>
        {patterns.length === 0 ? (
          <div className="aoip-state">
            Chưa có dữ liệu năng lực. Sổ ca chưa ghi nhận loại việc nào cho hệ thống của
            bạn — Omni cần chạy qua vài sự cố thật thì bảng này mới có nội dung.
          </div>
        ) : (
          <>
            <div className="aoip-muted" style={{ marginBottom: "0.6rem" }}>
              {patterns.length} loại việc · {duDieuKien} loại đã đủ điều kiện xin thêm quyền
            </div>
            <div className="aoip-table-wrap">
              <table className="aoip-table">
                <thead>
                  <tr>
                    <th>Loại việc</th>
                    <th>Đã nhận / Đã từ chối</th>
                    <th>Độ chính xác &amp; độ phủ (đi cùng nhau)</th>
                    <th>Quyền hiện tại</th>
                    <th>Đủ điều kiện xin thêm quyền?</th>
                  </tr>
                </thead>
                <tbody>
                  {patterns.map((p) => {
                    const blockers = Array.isArray(p?.blockers) ? p.blockers : [];
                    const diagnosed = soNguyen(p?.diagnosed);
                    const refused = soNguyen(p?.refused);
                    return (
                      <tr key={p.pattern_key}>
                        <td>
                          <code>{p.pattern_key}</code>
                          <div className="aoip-muted" style={{ fontSize: "var(--text-2xs)" }}>
                            {soNguyen(p?.total_cases)} ca đã ghi sổ ·{" "}
                            {phanTram(p?.recurrence_rate)} ca bị lặp lại
                          </div>
                        </td>
                        <td>
                          <div>
                            <strong>{diagnosed}</strong> nhận chẩn đoán
                          </div>
                          <div className="aoip-muted">{refused} từ chối</div>
                          {soNguyen(p?.out_of_scope) > 0 ? (
                            <div className="aoip-muted" style={{ fontSize: "var(--text-2xs)" }}>
                              {soNguyen(p.out_of_scope)} ca ngoài quyền hạn (không tính điểm)
                            </div>
                          ) : null}
                        </td>
                        <td>
                          <MetricPair
                            accuracy={p?.accuracy_lower_bound}
                            coverage={p?.coverage}
                            accuracyNote={`${soNguyen(p?.correct)} đúng / ${
                              soNguyen(p?.correct) + soNguyen(p?.incorrect) + soNguyen(p?.partial)
                            } ca đã chấm`}
                            coverageNote={`nhận ${diagnosed} / ${diagnosed + refused} ca được giao`}
                            footer={`Còn ${soNguyen(p?.unjudged)} ca chưa ai chấm đúng/sai`}
                          />
                        </td>
                        <td>
                          <span className="aoip-pill">{scopeLabel(p?.granted_scope)}</span>
                          {p?.frozen ? (
                            <div className="aoip-unavail" style={{ marginTop: "0.35rem" }}>
                              Đang đóng băng
                            </div>
                          ) : null}
                          {p?.frozen && p?.frozen_reason ? (
                            <div className="aoip-muted" style={{ fontSize: "var(--text-2xs)" }}>
                              {p.frozen_reason}
                            </div>
                          ) : null}
                        </td>
                        <td>
                          {p?.eligible ? (
                            <span className="aoip-ok">Đủ điều kiện</span>
                          ) : (
                            <>
                              <span className="aoip-unavail">Chưa đủ</span>
                              <ul className={styles.blockers}>
                                {blockers.length === 0 ? (
                                  <li>Chưa rõ lý do — liên hệ đơn vị vận hành.</li>
                                ) : (
                                  blockers.map((b, i) => (
                                    <li key={`${p.pattern_key}-${i}`}>{blockerDeHieu(b)}</li>
                                  ))
                                )}
                              </ul>
                            </>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="aoip-muted" style={{ marginTop: "0.7rem", fontSize: "var(--text-2xs)" }}>
              Cập nhật lúc {ngayGio(new Date().toISOString())}. Đủ điều kiện KHÔNG có nghĩa
              là quyền đã được mở — Omni phải nộp đơn và bạn phải đồng ý ở trang “Đơn xin
              quyền”.
            </div>
          </>
        )}
      </Card>
    </>
  );
}
