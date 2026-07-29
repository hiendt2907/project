import { headers } from "next/headers";
import { backendGet } from "@aoip/api-client";
import { Card } from "@aoip/ui-kit";
import { backendConfig } from "@/lib/config";
import {
  blockerDeHieu,
  ngayGio,
  phanTram,
  scopeLabel,
  soNguyen,
  stateLabel,
  type ScopeRequest,
} from "@/lib/competency";
import { MetricPair } from "../MetricPair";
import { DecideForm } from "./DecideForm";
import styles from "../ledger.module.css";

/**
 * "Đơn xin quyền" — Omni tự đề nghị được làm thêm việc, kèm bằng chứng.
 *
 * `evidence` là bản ĐÓNG BĂNG lúc nộp: nó có thể khác số hiện tại ở trang "Năng
 * lực", và đó là chủ ý — bạn duyệt đúng cái Omni đưa ra lúc xin, không phải một
 * con số đổi sau lưng.
 */
export default async function TenantScopeRequestsPage() {
  const resp = await backendGet(
    backendConfig,
    "/competency/scope-requests",
    (await headers()).get("cookie") ?? "",
  );
  if (!resp.ok) {
    return (
      <>
        <h1>Đơn xin quyền</h1>
        <Card error>
          <div className="aoip-state">
            Không tải được danh sách đơn ({resp.status}). Thử lại sau ít phút.
          </div>
        </Card>
      </>
    );
  }

  const body = (await resp.json()) as { tenant_id?: string; requests?: ScopeRequest[] };
  const requests = Array.isArray(body.requests) ? body.requests : [];
  const dangCho = requests.filter((r) => r?.state === "PENDING");
  const daXuLy = requests.filter((r) => r?.state !== "PENDING");

  return (
    <>
      <h1>Đơn xin quyền</h1>
      <p className="aoip-muted">
        Omni tự đề nghị được làm thêm một loại việc khi thấy mình đã có đủ ca để chứng
        minh. Bạn là người quyết định — không đồng ý thì không có gì thay đổi.
      </p>

      <p className={styles.note}>
        <strong>Nhìn cả hai con số trước khi bấm đồng ý.</strong> Độ tin cậy tối thiểu
        cao mà độ phủ thấp nghĩa là Omni chỉ nhận những ca dễ; mở quyền cho một trợ lý
        như vậy thì lúc cần nhất nó vẫn đứng ngoài. Số liệu dưới đây là bản chụp tại
        thời điểm nộp đơn, khớp với sổ ca và không sửa được về sau.
      </p>

      <h2>Đang chờ bạn</h2>
      <Card>
        {dangCho.length === 0 ? (
          <div className="aoip-state">
            Chưa có đơn nào đang chờ. Omni chỉ nộp đơn khi một loại việc đã đủ điều kiện —
            xem trang “Năng lực” để biết nó còn thiếu gì.
          </div>
        ) : (
          dangCho.map((r) => <RequestBlock key={r.id} req={r} decidable />)
        )}
      </Card>

      <h2>Đã xử lý</h2>
      <Card>
        {daXuLy.length === 0 ? (
          <div className="aoip-state">Chưa có đơn nào được xử lý.</div>
        ) : (
          daXuLy.map((r) => <RequestBlock key={r.id} req={r} />)
        )}
      </Card>
    </>
  );
}

function RequestBlock({ req, decidable }: { req: ScopeRequest; decidable?: boolean }) {
  const ev = req?.evidence && typeof req.evidence === "object" ? req.evidence : null;
  const blockers = Array.isArray(ev?.blockers) ? ev.blockers : [];

  return (
    <div className="aoip-question">
      <div className={styles.reqHead}>
        <span className={styles.reqTitle}>{req?.pattern_key ?? "—"}</span>
        <span className="aoip-pill">{stateLabel(req?.state)}</span>
      </div>
      <div className="aoip-muted">
        Xin được nâng lên: <strong>{scopeLabel(req?.requested_scope)}</strong> · nộp lúc{" "}
        {ngayGio(req?.created_at)}
      </div>

      <div className={styles.evidence}>
        {ev ? (
          <MetricPair
            accuracy={soNguyen(ev.accuracy_lower_bound)}
            coverage={soNguyen(ev.coverage)}
            accuracyNote={`${soNguyen(ev.correct)} đúng / ${
              soNguyen(ev.correct) + soNguyen(ev.incorrect) + soNguyen(ev.partial)
            } ca đã chấm`}
            coverageNote={`nhận ${soNguyen(ev.diagnosed)} / ${
              soNguyen(ev.diagnosed) + soNguyen(ev.refused)
            } ca được giao`}
            footer={`Bằng chứng đóng băng lúc nộp · ${soNguyen(ev.total_cases)} ca · ${phanTram(
              ev.recurrence_rate,
            )} ca bị lặp lại`}
          />
        ) : (
          <div className="aoip-state">Đơn này không kèm số liệu — không nên duyệt.</div>
        )}
      </div>

      {blockers.length > 0 ? (
        <ul className={styles.blockers}>
          {blockers.map((b, i) => (
            <li key={`${req.id}-${i}`}>{blockerDeHieu(b)}</li>
          ))}
        </ul>
      ) : null}

      {decidable ? (
        <DecideForm requestId={req.id} />
      ) : (
        <div className="aoip-muted" style={{ marginTop: "0.5rem", fontSize: "var(--text-xs)" }}>
          {req?.decided_by ? `${req.decided_by} quyết định` : "Đã quyết định"} lúc{" "}
          {ngayGio(req?.decided_at)}
          {req?.decision_note ? ` · “${req.decision_note}”` : ""}
          {req?.cooldown_until
            ? ` · khoá xin lại đến ${ngayGio(req.cooldown_until)}`
            : ""}
        </div>
      )}
    </div>
  );
}
