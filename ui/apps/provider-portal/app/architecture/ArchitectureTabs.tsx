"use client";

import { useState, type ReactNode } from "react";

// CHỈ presentation: ba bản vẽ được render ở page.tsx (server component) rồi truyền
// xuống dưới dạng ReactNode. React element là lazy — panel không được chọn thì không
// mount, nên 6 sơ đồ Mermaid ở tab kỹ thuật chỉ dựng khi người dùng mở đúng tab đó.

export type ArchView = "technical" | "plain" | "questions";

interface TabDef {
  id: ArchView;
  n: string;
  label: string;
  hint: string;
}

const TABS: TabDef[] = [
  { id: "plain", n: "01", label: "Bản dễ hiểu", hint: "Không cần biết kỹ thuật — Omni làm gì, chạy ở đâu" },
  { id: "technical", n: "02", label: "Sơ đồ kỹ thuật", hint: "6 sơ đồ + nguồn xác minh từng node" },
  { id: "questions", n: "03", label: "Ba câu hỏi bằng số đo", hint: "6 cổng · phễu 818 trace · vòng học" },
];

interface ArchitectureTabsProps {
  technical: ReactNode;
  plain: ReactNode;
  questions: ReactNode;
}

export function ArchitectureTabs({ technical, plain, questions }: ArchitectureTabsProps) {
  const [active, setActive] = useState<ArchView>("plain");

  return (
    <div className="arch">
      <div className="arch-tabs" role="tablist" aria-label="Chọn bản vẽ kiến trúc">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`arch-tab-${t.id}`}
            aria-selected={active === t.id}
            aria-controls={`arch-panel-${t.id}`}
            className="arch-tab"
            data-testid={`arch-tab-${t.id}`}
            onClick={() => setActive(t.id)}
          >
            <span className="arch-tab-n">{t.n}</span>
            <span className="arch-tab-l">{t.label}</span>
            <span className="arch-tab-d">{t.hint}</span>
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`arch-panel-${active}`} aria-labelledby={`arch-tab-${active}`}>
        {active === "plain" && plain}
        {active === "technical" && technical}
        {active === "questions" && questions}
      </div>
    </div>
  );
}
