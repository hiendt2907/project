import { headers } from "next/headers";
import { Card, MetricStat } from "@aoip/ui-kit";
import { fetchAudit } from "@/lib/audit";
import { PageIntro } from "@/components/PageIntro";

// Read-only projection of services.audit_ledger.chain_writer (CRAT tamper-evident
// hash-chain) — every event type (ADVISORY_DECISION/ADVISORY_DISPATCHED/
// MUTATION_TRAPPED/HITL_DECISION/ROLLBACK_EXECUTED), across tenants. Distinct from
// /incidents (SIEM-only verdict view, per-tenant): this is the full compliance chain.
export default async function ProviderAuditPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const result = await fetchAudit(cookieHeader);

  if (result.status === "error") {
    return (
      <Card error>
        <div className="aoip-k err">Không tải được Audit chain</div>
        <div className="aoip-state" data-testid="audit-error">
          Backend trả mã {result.code || "không phản hồi"}. Thử tải lại trang.
        </div>
      </Card>
    );
  }

  const { total, signed, event_counts, blocks } = result.data;
  return (
    <>
      <PageIntro
        title="Sổ kiểm toán"
        lead="Đây là cuốn sổ ghi lại MỌI quyết định quan trọng của hệ thống: đã chẩn đoán gì, đã gửi khuyến nghị nào, đã chặn thao tác nguy hiểm nào, ai đã duyệt gì. Sổ dùng khoá mật mã nối các trang với nhau — đã ghi là không xoá/sửa được, đáp ứng chuẩn kiểm toán tài chính (SOX, PCI-DSS)."
        terms={[
          { term: "Block / Seq", meaning: "Một trang sổ và số thứ tự của nó. Các trang nối nhau bằng mã khoá nên rút hay sửa một trang sẽ bị phát hiện ngay." },
          { term: "Signed (đã ký)", meaning: "Trang sổ có chữ ký điện tử của hệ thống — thêm một lớp chống giả mạo." },
          { term: "ADVISORY_DECISION", meaning: "Hệ thống đưa ra một chẩn đoán + khuyến nghị." },
          { term: "MUTATION_TRAPPED", meaning: "Một thao tác thay đổi hệ thống bị chặn lại bởi công tắc an toàn." },
          { term: "HITL_DECISION", meaning: "Con người đã duyệt hoặc từ chối một đề xuất." },
        ]}
      />
      <div className="aoip-grid" data-testid="audit-summary">
        <MetricStat label="Blocks (gần nhất)" value={total} />
        <MetricStat label="Đã ký (signed)" value={signed} />
        {Object.entries(event_counts).map(([type, count]) => (
          <MetricStat key={type} label={type} value={count} />
        ))}
      </div>

      <Card>
        {blocks.length === 0 ? (
          <div className="aoip-state" data-testid="audit-empty">
            Chưa có audit block nào.
          </div>
        ) : (
          <div className="aoip-table-wrap">
            <table className="aoip-table" data-testid="audit-table">
              <thead>
                <tr>
                  <th>Seq</th>
                  <th>Event</th>
                  <th>Tenant</th>
                  <th>Trace</th>
                  <th>Timestamp</th>
                  <th>Hash</th>
                </tr>
              </thead>
              <tbody>
                {blocks.map((b) => (
                  <tr key={`${b.tenant_id}-${b.seq}`} data-testid={`audit-block-${b.tenant_id}-${b.seq}`}>
                    <td>{b.seq}</td>
                    <td>
                      {b.event_type}
                      {b.signed ? <span className="aoip-muted"> · signed</span> : null}
                    </td>
                    <td>{b.tenant_id}</td>
                    <td className="aoip-muted">{b.trace_id || "n/a"}</td>
                    <td className="aoip-muted">{b.timestamp_utc}</td>
                    <td className="aoip-muted">{b.block_hash.slice(0, 12)}…</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
