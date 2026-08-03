import { PageIntro } from "@/components/PageIntro";
import { ArchitectureTabs } from "./ArchitectureTabs";
import { TechnicalView } from "./TechnicalView";
import { PlainView } from "./PlainView";
import { QuestionsView } from "./QuestionsView";
import { MEASURED_AT } from "./diagrams";
import "./architecture.css";

// NGOẠI LỆ CÓ CHỦ ĐÍCH so với GOVERNING RULE ở lib/nav.ts: trang này KHÔNG phải
// read-projection từ runtime — nó là ẢNH CHỤP kiến trúc đo ngày 2026-08-02, nội dung
// nằm tĩnh trong ./diagrams.ts + 3 view component. Vì thế nó KHÔNG tự cập nhật khi
// cluster đổi. Đổi lại, mỗi con số đều đi kèm lệnh đã dùng để đo (bảng "nguồn xác
// minh"), nên người đọc kiểm chứng lại được thay vì phải tin. Muốn số mới: đo lại rồi
// sửa diagrams.ts — KHÔNG suy diễn, KHÔNG làm tròn khác đi.
//
// Server component: 3 bản vẽ render server-side rồi truyền xuống ArchitectureTabs
// (client, chỉ giữ state tab đang chọn). Chỉ MermaidBlock chạy trên client.

export const metadata = {
  title: "Bản vẽ kiến trúc · AOIP Provider Operations",
  description: `Ba bản vẽ kiến trúc Omni, đo ngày ${MEASURED_AT}`,
};

export default function ArchitecturePage() {
  return (
    <>
      <PageIntro
        title="Bản vẽ kiến trúc"
        lead={
          `Ba cách nhìn cùng một hệ thống, đo trực tiếp từ cụm và máy khách ngày ${MEASURED_AT}. ` +
          "«Bản dễ hiểu» dành cho người không rành kỹ thuật. «Sơ đồ kỹ thuật» có 6 sơ đồ kèm lệnh đã dùng " +
          "để xác minh từng node. «Ba câu hỏi bằng số đo» cho biết thực tế đã chạy được tới đâu — " +
          "kể cả những chỗ chưa chạy. Đây là ảnh chụp tĩnh, không phải màn hình theo dõi thời gian thực."
        }
        terms={[
          { term: "Evidence → Incident", meaning: "Bằng chứng thô (số đo, log) chỉ trở thành sự cố khi qua đủ 6 cổng kiểm — trượt một cổng là dừng." },
          { term: "Trace", meaning: "Một lần hệ thống xử lý tín hiệu, ghi lại đã đi qua những chặng nào." },
          { term: "Kill-switch", meaning: "Công tắc tổng chặn mọi hành động tự sửa. Đang TẮT — Omni chỉ quan sát và báo, không tự động chạm vào hệ thống thật." },
          { term: "CRAT", meaning: "Sổ kiểm toán ký bằng mật mã, nối chuỗi băm. Ghi sổ hỏng thì dừng toàn bộ, không phát thẻ sự cố." },
          { term: "Drift", meaning: "Chỗ tài liệu nói khác thực tế đo được. Liệt kê ra để quyết định, chưa sửa." },
        ]}
      />
      <ArchitectureTabs
        plain={<PlainView />}
        technical={<TechnicalView />}
        questions={<QuestionsView />}
      />
    </>
  );
}
