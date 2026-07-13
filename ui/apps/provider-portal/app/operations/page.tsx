import { headers } from "next/headers";
import { Card, KeyVal } from "@aoip/ui-kit";
import { PageIntro } from "@/components/PageIntro";
import { fetchOperations } from "@/lib/operations";

export default async function OperationsPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const result = await fetchOperations(cookieHeader);

  return (
    <>
      <PageIntro
        title="Việc cần xử lý"
        lead="Danh sách các sự cố đang cần con người để mắt tới: hoặc chưa gửi được báo cáo cho khách hàng, hoặc trạng thái thực tế và sổ sách chưa khớp nhau (cần đối soát). Danh sách trống là trạng thái tốt — mọi việc đã xong hoặc đang tự chạy đúng tiến độ."
        terms={[
          { term: "Cần đối soát (reconcile)", meaning: "Hệ thống chưa chắc chắn thao tác đã hoàn tất hay chưa — cần kiểm tra lại trước khi làm gì tiếp, tuyệt đối không tự làm lại một cách mù quáng." },
          { term: "Giai đoạn (phase)", meaning: "Sự cố đang ở bước nào trong quy trình xử lý." },
        ]}
      />

      {result.status === "error" ? (
        <Card error>
          <div className="aoip-k err">Không tải được danh sách việc cần xử lý</div>
          <div className="aoip-state" data-testid="operations-error">
            Backend trả mã {result.code || "không phản hồi"}. Thử tải lại trang.
          </div>
        </Card>
      ) : result.data.operations.length === 0 ? (
        <Card>
          <div className="aoip-state" data-testid="operations-empty">
            Không có việc nào đang chờ — mọi sự cố đã được báo cáo và trạng thái đều khớp sổ sách.
          </div>
        </Card>
      ) : (
        result.data.operations.map((op) => (
          <Card key={`${op.tenant}:${op.correlation_id}`}>
            <KeyVal label="Khách hàng">{op.tenant}</KeyVal>
            <KeyVal label="Mã sự cố">{op.correlation_id}</KeyVal>
            <KeyVal label="Giai đoạn">{op.phase}</KeyVal>
            <KeyVal label="Cần làm gì">
              {op.reconcile_required
                ? "Cần đối soát: kiểm tra lại trạng thái thực tế trước khi tiếp tục."
                : "Chưa gửi được báo cáo cho khách hàng — cần theo dõi."}
            </KeyVal>
          </Card>
        ))
      )}
    </>
  );
}
