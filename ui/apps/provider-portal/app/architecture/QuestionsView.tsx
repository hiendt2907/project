import { MEASURED_AT } from "./diagrams";

/** Bản vẽ 3 — ba câu hỏi trả lời bằng SỐ ĐO, không bằng thiết kế.
 *  Mỗi phần có hai lớp: luật trong mã (điều kiện chính xác để đi tiếp) và số thật
 *  (thực tế đã xảy ra bao nhiêu lần). Số nguyên văn từ lần đo 2026-08-02 — không
 *  làm tròn lại, không suy diễn thêm. */

interface Gate {
  i: number;
  title: string;
  intro?: string;
  /** Điều kiện nguyên văn, giữ xuống dòng (render white-space: pre). */
  cond?: string;
  /** Hệ quả khi trượt cổng. */
  drop?: string;
  /** Số đo thật tại thời điểm đo. */
  meas?: string;
  body?: string[];
}

const GATES: Gate[] = [
  {
    i: 1,
    title: "Cửa nhận phân loại theo nhãn tự khai",
    intro: "Ngay tại cổng HTTP, chưa vào worker nào cả.",
    cond: 'signal_type == "ANOMALY" → omni-diagnostic-evidence\nngược lại              → omni-knowledge-evidence',
    drop: "Không mang nhãn ANOMALY thì không bao giờ chạm tới RAG hay LLM.",
  },
  {
    i: 2,
    title: "Ai được quyền phán, tuỳ vào độ tin của baseline",
    intro:
      "Mỗi cặp khách × máy có một điểm tin cậy 0–100, cộng 1 điểm mỗi 100 mẫu, trừ 5 điểm mỗi ngày không học.",
    meas: "Đo thật: cust-db 86 · cust-edge 84 · cust-app 78 điểm. Đã thu 7.800–8.600 mẫu mỗi máy.",
  },
  {
    i: 3,
    title: "Điều kiện coi là lệch",
    intro: "Ba luật cùng lúc, mỗi luật sinh ra từ một lần báo động giả đã trả giá.",
    cond: "z ≥ 3.0  VÀ  giá trị ≥ sàn biên độ   (cpu 60% · mem 70%)\nhoặc   giá trị ≥ hàng rào tĩnh của khách",
    body: [
      "Chỉ xét lệch phía trên. CPU tụt sâu không phải sự cố tài nguyên.",
      "Đĩa bị loại khỏi z-score — đĩa tăng đơn điệu nên z phẳng lúc bò 70→99% và bung lúc 40→41%. Đĩa chỉ do hàng rào tĩnh quyết.",
      "Hàng rào tĩnh là cận trên ở mọi bậc, kể cả AUTONOMOUS — trước đây tắt nó ở bậc cao khiến đĩa 99% không báo gì, càng tin máy càng mù.",
    ],
  },
  {
    i: 4,
    title: "Chống lặp",
    cond: "khoá: {khách}:{máy}:{metric} — giữ 600 giây",
    intro: "Không có khoá này, một máy CPU cao liên tục sẽ bơm một sự cố mới mỗi 60 giây.",
    meas: "Đo lúc này: 0 khoá đang sống — 10 phút qua không có lần nâng cấp nào.",
  },
  {
    i: 5,
    title: "Nâng cấp và chấm mức nghiêm trọng",
    intro:
      'Bản ghi được đóng dấu result = "FAILED" — đúng chuỗi này, thiếu là chết lặng ở bước chấm điểm. ' +
      "Lĩnh vực lấy theo METRIC, không theo bản ghi chứa nó: một bản ghi gộp CPU/RAM/đĩa dưới nhãn os_host, " +
      "lấy nhãn của bản ghi thì đĩa đầy sẽ gọi nhầm bộ chẩn đoán.",
    cond:
      "FAILED + database                   → critical\nFAILED + service|os|storage|network → high\nOOM > 0 · cpu > 95 · mem > 95       → critical",
  },
  {
    i: 6,
    title: "Cổng cuối — có đáng làm phiền người không",
    cond: "urgency ∈ { critical, high }   → gửi thẻ\nmedium trở xuống               → im lặng",
    intro:
      "Cộng thêm hai bộ lọc lặp: cụm đã thấy rồi mà mức chưa leo lên critical/high thì bỏ qua, không tạo trace mới.",
    drop: "Đây là lý do sự cố mức medium không bao giờ tới Telegram, dù đã đi hết bảy bước trên.",
  },
];

const BANDS: { r: string; n: string; d: string; live?: boolean }[] = [
  { r: "0 – 24", n: "STATIC_GUARD", d: "Chỉ hàng rào tĩnh." },
  { r: "25 – 49", n: "LEARNING", d: "Vẫn hàng rào tĩnh; z-score chỉ ghi sổ đối chiếu." },
  { r: "50 – 74", n: "ASSISTED", d: "z-score là chính." },
  { r: "75 – 100", n: "AUTONOMOUS", d: "z-score quyết định.", live: true },
];

/** Phễu 13 chặng, đếm trên toàn bộ 818 trace đang lưu. `pct` là chuỗi nguyên văn
 *  từ lần đo (dấu phẩy thập phân tiếng Việt) — KHÔNG tính lại từ count. */
const FUNNEL: { stage: string; count: number; pct: string; width: number }[] = [
  { stage: "EVIDENCE", count: 818, pct: "100%", width: 100 },
  { stage: "INGEST", count: 12, pct: "1,5%", width: 1.5 },
  { stage: "RAG", count: 12, pct: "1,5%", width: 1.5 },
  { stage: "LLM", count: 12, pct: "1,5%", width: 1.5 },
  { stage: "VERIFY", count: 4, pct: "0,5%", width: 0.5 },
  { stage: "SCHEMA", count: 4, pct: "0,5%", width: 0.5 },
  { stage: "KILLSWITCH", count: 4, pct: "0,5%", width: 0.5 },
  { stage: "CRAT", count: 4, pct: "0,5%", width: 0.5 },
  { stage: "DISPATCH", count: 12, pct: "1,5%", width: 1.5 },
  { stage: "HITL", count: 12, pct: "1,5%", width: 1.5 },
  { stage: "EXECUTOR", count: 12, pct: "1,5%", width: 1.5 },
  { stage: "FEEDBACK", count: 12, pct: "1,5%", width: 1.5 },
];

const STAGE_TABLE: { stage: string; does: string; blocks: string }[] = [
  { stage: "1 INGEST", does: "Nhận vào hàng đợi", blocks: "—" },
  { stage: "2 EVIDENCE", does: "Gom dấu hiệu cùng lớp thành một cụm", blocks: "Cụm cũ + mức chưa leo lên critical/high" },
  { stage: "3 RAG", does: "Tra sổ tay 1.019 mục", blocks: "Khớp ≥ 0,75 thì dùng lại lời cũ, bỏ qua LLM" },
  { stage: "4 LLM", does: "Sinh chẩn đoán: chuyện gì / ai chịu / vì sao / làm gì / dự báo", blocks: "Mức dưới critical–high thì không gọi" },
  { stage: "5 VERIFY", does: "Đối chiếu kết luận với bằng chứng vật lý", blocks: "Thiếu bằng chứng loại bắt buộc thì chặn" },
  { stage: "6 SCHEMA", does: "Ép về đúng khuôn máy đọc được", blocks: "Sai khuôn thì bỏ" },
  { stage: "7 KILLSWITCH", does: "Kiểm công tắc tổng", blocks: "Đang TẮT — mọi đường ra tay dừng ở đây" },
  { stage: "8 CRAT", does: "Băm, ký, nối vào sổ không sửa được", blocks: "Ký hỏng thì dừng toàn bộ, không gửi gì" },
  { stage: "9 DISPATCH", does: "Gửi thẻ tiếng Việt qua Telegram", blocks: "Dưới ngưỡng làm phiền" },
  { stage: "10 HITL", does: "Chờ người bấm đồng ý / bác bỏ", blocks: "Chế độ chỉ-gợi-ý thì bỏ qua hàng chờ" },
  { stage: "11 EXECUTOR", does: "Ra tay sửa thật", blocks: "Công tắc tắt — chưa từng chạy" },
  { stage: "12 FEEDBACK", does: "Xem sửa xong có đỡ không, đánh giá lại", blocks: "Không có hành động thì không có phản hồi" },
  { stage: "13 AUTO_RECOVERY", does: "Tự phục hồi khi mẫu đã đủ tin", blocks: "Chưa dùng" },
];

export function QuestionsView() {
  return (
    <div className="arch-panel" data-testid="arch-questions">
      <section className="arch-sec">
        <div className="arch-sec-head">
          <span className="arch-eyebrow">Bản vẽ 3 · trả lời bằng số đo</span>
          <h2 className="arch-h2">Ba câu hỏi, trả lời bằng số đo chứ không bằng thiết kế</h2>
          <p className="arch-p">
            Mỗi phần có hai lớp: <strong>luật trong mã</strong> — điều kiện chính xác để đi tiếp, và{" "}
            <strong>số thật</strong> — thực tế đã xảy ra bao nhiêu lần. Hai lớp này không trùng nhau, và chỗ
            chúng lệch mới là chỗ đáng đọc. Đo trực tiếp trên cụm và VM · {MEASURED_AT}.
          </p>
        </div>
      </section>

      {/* ── Câu 1 ─────────────────────────────────────────────────────────── */}
      <section className="arch-sec">
        <div className="arch-sec-head">
          <span className="arch-eyebrow">Câu hỏi 1</span>
          <h2 className="arch-h2">Khi nào một evidence trở thành incident</h2>
          <p className="arch-p">Sáu cổng nối tiếp. Trượt bất kỳ cổng nào là dừng — không có đường vòng.</p>
        </div>
        <div className="arch-gates">
          {GATES.map((g) => (
            <article className="arch-gate" key={g.i}>
              <span className="arch-gate-i">{g.i}</span>
              <div className="arch-gate-in">
                <h3 className="arch-h3">{g.title}</h3>
                {g.intro && <p className="arch-p">{g.intro}</p>}
                {g.i === 2 && (
                  <div className="arch-bands">
                    {BANDS.map((b) => (
                      <div className={`arch-band${b.live ? " live" : ""}`} key={b.n}>
                        <span className="arch-band-r">{b.r}</span>
                        <span className="arch-band-n">{b.n}</span>
                        <span className="arch-band-d">{b.d}</span>
                        {b.live && <span className="arch-band-now">◆ cả 3 máy đang ở đây</span>}
                      </div>
                    ))}
                  </div>
                )}
                {g.cond && <pre className="arch-cond">{g.cond}</pre>}
                {g.body?.map((line) => (
                  <p className="arch-p" key={line}>
                    {line}
                  </p>
                ))}
                {g.drop && <span className="arch-drop">{g.drop}</span>}
                {g.meas && <span className="arch-meas">{g.meas}</span>}
              </div>
            </article>
          ))}
        </div>
      </section>

      {/* ── Câu 2 ─────────────────────────────────────────────────────────── */}
      <section className="arch-sec">
        <div className="arch-sec-head">
          <span className="arch-eyebrow">Câu hỏi 2</span>
          <h2 className="arch-h2">Một incident đi hết luồng trông ra sao — và bao nhiêu cái thật sự đi hết</h2>
          <p className="arch-p">
            Luồng đầy đủ có 13 chặng. Dưới đây là số trace thật đã chạm từng chặng, đếm trên toàn bộ{" "}
            <strong>818 trace</strong> đang lưu.
          </p>
        </div>

        <div className="arch-funnel" data-testid="arch-funnel">
          {FUNNEL.map((f) => (
            <div className="arch-fr" key={f.stage}>
              <span className="arch-fr-l">{f.stage}</span>
              <div className="arch-fr-track">
                <div className="arch-fr-bar" style={{ width: `${f.width}%` }} />
              </div>
              <span className="arch-fr-v">
                {f.count} <small>{f.pct}</small>
              </span>
            </div>
          ))}
          <div className="arch-fr total">
            <span className="arch-fr-l">thật sự có chẩn đoán</span>
            <div className="arch-fr-track">
              <div className="arch-fr-bar none" style={{ width: "100%" }} />
            </div>
            <span className="arch-fr-v">
              0 <small>0%</small>
            </span>
          </div>
        </div>

        <div className="arch-prose">
          <h3 className="arch-h3">Đọc cái phễu này</h3>
          <p className="arch-p">
            <strong>806 trace dừng ngay ở chặng đầu.</strong> Mở ra xem thì tất cả đều là{" "}
            <code className="arch-code">ONBOARDING_DISCOVERY</code> — dò cổng, dò dịch vụ, vẽ bản đồ hệ thống
            khách. Đây là việc <em>tìm hiểu</em>, không phải sự cố, nên dừng ở đó là đúng.
          </p>
          <p className="arch-p">
            <strong>12 trace đi xa nhất đều không phải sự cố của khách.</strong> Toàn bộ mang tiền tố{" "}
            <code className="arch-code">gw-prom-</code>: cảnh báo Omni tự phát về chính mình, tên{" "}
            <code className="arch-code">OmniAdvisoryAcceptanceLow</code> — «tỉ lệ người đồng ý với tôi đang thấp».
          </p>
          <p className="arch-p">
            <strong>Và ngay cả 12 cái đó cũng không được chẩn đoán.</strong> Đọc chi tiết từng chặng thì thấy:
            RAG <em>skip</em>, LLM <em>skip</em>, HITL <em>skip</em>, EXECUTOR <em>skip</em> — lý do ghi rõ là{" "}
            <code className="arch-code">meta_self — deduped</code> và{" "}
            <code className="arch-code">suggest-only — no mutate</code>.
          </p>
        </div>

        <p className="arch-note alarm">
          <span className="arch-note-t">Kết luận thẳng</span>
          Trong toàn bộ 818 trace đang lưu, <strong>không có một sự cố khách hàng nào đi hết luồng</strong>, và{" "}
          <strong>không lần nào RAG + LLM thực sự chạy để chẩn đoán</strong>. Bốn trace chạm được sổ ký CRAT là
          bốn cảnh báo Omni nói về bản thân, xử theo đường tất định chứ không qua suy luận.
        </p>

        <div className="arch-prose">
          <h3 className="arch-h3">Vậy luồng đầy đủ trên lý thuyết là gì</h3>
        </div>
        <div className="aoip-table-wrap">
          <table className="aoip-table" data-testid="arch-stage-table">
            <thead>
              <tr>
                <th>Chặng</th>
                <th>Làm gì</th>
                <th>Chặn khi</th>
              </tr>
            </thead>
            <tbody>
              {STAGE_TABLE.map((s) => (
                <tr key={s.stage}>
                  <td>
                    <code className="arch-code">{s.stage}</code>
                  </td>
                  <td>{s.does}</td>
                  <td>{s.blocks}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* ── Câu 3 ─────────────────────────────────────────────────────────── */}
      <section className="arch-sec">
        <div className="arch-sec-head">
          <span className="arch-eyebrow">Câu hỏi 3</span>
          <h2 className="arch-h2">Vòng học đang ra sao</h2>
          <p className="arch-p">
            Có <strong>hai</strong> đường học tách biệt. Một đường chết đúng thiết kế, một đường đang sống nhưng lệch.
          </p>
        </div>

        <div className="arch-paths">
          <article className="arch-path dead">
            <header className="arch-path-cap">
              <h3 className="arch-h3">Đường A — học qua việc đã làm</h3>
              <small>chết trong chế độ hiện tại</small>
            </header>
            <div className="arch-path-in">
              <p className="arch-p">
                Chỉ kích hoạt sau khi một hành động sửa thật được xác nhận thành công. Công tắc tự ra tay đang
                TẮT ⇒ không bao giờ có hành động ⇒ không bao giờ học.
              </p>
              <p className="arch-p">
                <strong>Đây không phải bug.</strong> Đó là hệ quả trực tiếp của việc chọn chế độ an toàn.
              </p>
              <span className="arch-meas dead">
                Đo thật: 0 khoá <code className="arch-code">omni:learn:promo:*</code> trong bộ nhớ nhanh.
              </span>
            </div>
          </article>

          <article className="arch-path live">
            <header className="arch-path-cap">
              <h3 className="arch-h3">Đường B — học qua phán quyết của người</h3>
              <small>đang chạy</small>
            </header>
            <div className="arch-path-in">
              <p className="arch-p">
                Người bấm nút trên thẻ Telegram: <em>đúng</em> / <em>sai</em> / <em>đúng nhưng thiếu</em>. «Đúng
                nhưng thiếu» cố ý không thưởng cũng không phạt — nó nói về độ đầy đủ, không nói chẩn đoán sai.
              </p>
              <p className="arch-p">Các sự cố cùng lớp triệu chứng được gom theo một khoá băm, rồi lên bậc dần.</p>
              <p className="arch-p">
                <strong>Rào an toàn:</strong> tốt nghiệp theo đường này <em>luôn</em> giữ cờ không-tự-thực-thi.
                Người đồng ý với một <em>chẩn đoán</em> không phải là uỷ quyền cho máy <em>tự làm</em>.
              </p>
            </div>
          </article>
        </div>

        <div className="arch-prose">
          <h3 className="arch-h3">Bậc tốt nghiệp và thực tế đã chạm tới đâu</h3>
        </div>
        <div className="arch-fsm">
          <span className="arch-st">DRAFT</span>
          <span className="arch-arr">→</span>
          <span className="arch-st hit">CANDIDATE · 1 mẫu</span>
          <span className="arch-arr">→</span>
          <span className="arch-st hit">GRADUATED · 2 mẫu</span>
          <span className="arch-arr" style={{ marginLeft: "0.6rem" }}>
            còn nhánh phạt:
          </span>
          <span className="arch-st never">FROZEN · chưa bao giờ</span>
        </div>

        <div className="arch-stats">
          <div className="arch-stat">
            <span className="arch-stat-k">nhãn khen đã ghi</span>
            <span className="arch-stat-v ok">7</span>
            <span className="arch-stat-m">trên 3 lớp triệu chứng</span>
          </div>
          <div className="arch-stat">
            <span className="arch-stat-k">nhãn chê đã ghi</span>
            <span className="arch-stat-v stop">0</span>
            <span className="arch-stat-m">nhánh đóng băng chưa từng chạy</span>
          </div>
          <div className="arch-stat">
            <span className="arch-stat-k">ngưỡng tốt nghiệp</span>
            <span className="arch-stat-v">3</span>
            <span className="arch-stat-m">khen liên tiếp, chê quá 25% thì đóng băng</span>
          </div>
          <div className="arch-stat">
            <span className="arch-stat-k">sổ ký nhận trên cổng</span>
            <span className="arch-stat-v warn">1</span>
            <span className="arch-stat-m">so với 7 lần học — hai sổ không khớp</span>
          </div>
        </div>

        <div className="arch-note alarm">
          <span className="arch-note-t">Chỗ lệch nghiêm trọng nhất, và nó nối ngược lại câu hỏi 2</span>
          <p className="arch-p" style={{ marginBottom: "0.5rem" }}>
            Có <strong>hai quyển sổ ghi cùng một việc</strong> nhưng nói ngược nhau:
          </p>
          <div className="arch-chain">
            <div>
              <span className="arch-chain-k">A</span>
              <span className="arch-p">
                Sổ tốt nghiệp trong cơ sở dữ liệu: <strong>7 khen, 0 chê</strong> → 3 lớp triệu chứng lên bậc.
              </span>
            </div>
            <div>
              <span className="arch-chain-k">B</span>
              <span className="arch-p">
                Sổ đo hiệu quả trong bộ nhớ nhanh: <strong>0 khen, 4 chê</strong> — khoá «khen»{" "}
                <strong>không tồn tại</strong>.
              </span>
            </div>
          </div>
          <p className="arch-p" style={{ marginTop: "0.5rem" }}>
            Tỉ lệ đồng thuận tính từ sổ B ra <strong>0%</strong>. Omni đọc con số đó, kết luận mình đang kém, rồi
            tự phát cảnh báo <code className="arch-code">OmniAdvisoryAcceptanceLow</code> về chính mình.
          </p>
          <p className="arch-p" style={{ marginTop: "0.4rem" }}>
            <strong>Và đó chính là 11 trong 12 trace đi xa nhất ở câu hỏi 2.</strong> Nói cách khác: thứ được xử
            lý sâu nhất trong toàn hệ thống hiện nay là lời than phiền của Omni về điểm số của chính nó — một
            điểm số sai vì sổ đếm khen không có ai ghi vào.
          </p>
        </div>

        <div className="arch-prose">
          <h3 className="arch-h3">Ba câu hỏi này nối thành một vòng</h3>
          <p className="arch-p">
            Sự cố khách không đi hết luồng <em>(câu 2)</em> ⇒ không có thẻ để người phán ⇒ sổ khen trống{" "}
            <em>(câu 3)</em> ⇒ điểm tự đánh giá bằng 0 ⇒ Omni sinh cảnh báo về mình ⇒ cảnh báo đó chiếm hết phần
            luồng còn hoạt động <em>(câu 2 lần nữa)</em>. Vòng này tự nuôi nó.
          </p>
        </div>
      </section>

      <footer className="arch-foot">
        Nguồn số: đếm chặng trên toàn bộ 818 khoá trace trong bộ nhớ nhanh · truy vấn bảng tốt nghiệp và bảng ký
        nhận trong cơ sở dữ liệu · đọc điểm tin cậy và bộ đếm mẫu theo từng máy · đọc biến môi trường trong tiến
        trình đang chạy. Luật trích từ mã nguồn, không từ tài liệu. Bản kỹ thuật có neo tới từng dòng:{" "}
        <code className="arch-code">docs/architecture/SYSTEM_DIAGRAMS.md</code>.
      </footer>
    </div>
  );
}
