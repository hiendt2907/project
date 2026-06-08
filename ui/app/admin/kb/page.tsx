import { KnowledgeBasePanel } from "@/components/admin/KnowledgeBasePanel";

export const dynamic = "force-dynamic";

export default function KbPage() {
  return (
    <div className="p-4">
      <KnowledgeBasePanel />
    </div>
  );
}
