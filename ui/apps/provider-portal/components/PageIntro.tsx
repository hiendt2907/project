import "./page-intro.css";

interface Term {
  term: string;
  meaning: string;
}

interface PageIntroProps {
  /** Tiêu đề trang, tiếng Việt, ngắn. */
  title: string;
  /** 1-3 câu trả lời "trang này cho biết gì / vì sao quan trọng" — ngôn ngữ đời thường,
   *  không giả định người đọc biết khái niệm kỹ thuật. */
  lead: string;
  /** Chú giải thuật ngữ xuất hiện trên trang (mở/đóng được, mặc định đóng). */
  terms?: Term[];
}

/** Lớp giải thích phi kỹ thuật đặt đầu MỌI trang portal: người không rành hệ thống
 *  đọc 2 câu đầu là hiểu trang dùng để làm gì; thuật ngữ chuyên môn có chú giải
 *  ngay tại chỗ thay vì bắt người dùng tra cứu. */
export function PageIntro({ title, lead, terms }: PageIntroProps) {
  return (
    <header className="aoip-intro" data-testid="page-intro">
      <h1 className="aoip-intro-title">{title}</h1>
      <p className="aoip-intro-lead">{lead}</p>
      {terms && terms.length > 0 && (
        <details className="aoip-intro-glossary">
          <summary>Giải thích thuật ngữ trên trang này</summary>
          <dl>
            {terms.map((t) => (
              <div key={t.term} className="aoip-intro-term">
                <dt>{t.term}</dt>
                <dd>{t.meaning}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </header>
  );
}
