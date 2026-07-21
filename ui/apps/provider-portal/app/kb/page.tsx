import { Card } from "@aoip/ui-kit";
import { PageIntro } from "@/components/PageIntro";
import { KnowledgeBasePanel } from "./KnowledgeBasePanel";

// Ported from ui/app/admin/kb + ui/components/admin/KnowledgeBasePanel.tsx. The RAG
// vendor-knowledge store has no tenant_id (src/gateway/routes/kb.py) — this is a
// provider-only (vendor-ops) tool, not a per-tenant projection, so unlike most pages
// here there is no per-tenant loop; the panel itself talks straight to the gateway
// through /api/gateway/kb (client-side, since search/add/delete are interactive).

export const dynamic = "force-dynamic";

export default function ProviderKbPage() {
  return (
    <>
      <PageIntro
        title="Kho tri thức (RAG)"
        lead="Đây là những gì Omni «đã học» để chẩn đoán sự cố — mẹo vận hành, cách xử lý theo từng hãng phần mềm, kinh nghiệm rút ra từ các lần xử lý trước. Thêm một mục mới ở đây nghĩa là Omni sẽ tham khảo nó ngay từ lần chẩn đoán tiếp theo."
        terms={[
          { term: "RAG", meaning: "Cách Omni tra cứu tri thức liên quan trước khi trả lời — giống tra cẩm nang thay vì đoán mò." },
          { term: "Bộ sưu tập (collection)", meaning: "Một ngăn tri thức riêng (ví dụ: kiến thức Kubernetes, SOP nội bộ...). Mục mới luôn vào ngăn dùng để ghi (vendor_knowledge)." },
          { term: "Điểm chất lượng (score)", meaning: "Omni tin tưởng một mục tri thức tới đâu (0-100) — điểm càng cao càng được ưu tiên tham khảo." },
          { term: "Cấp (tier)", meaning: "Độ khó/độ sâu của kiến thức: cơ bản, trung cấp, nâng cao." },
        ]}
      />
      <Card>
        <KnowledgeBasePanel />
      </Card>
    </>
  );
}
