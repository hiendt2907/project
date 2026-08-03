import { MEASURED_AT } from "./diagrams";

/** Bản vẽ 2 — cùng hệ thống, không có tên module. Nguồn: bản giải thích tiếng Việt
 *  thường dựng ngày 2026-08-02. Số liệu giữ nguyên, không làm tròn lại. */

interface Step {
  n: string;
  title: string;
  body: string;
  who: string;
}

const STEPS: Step[] = [
  {
    n: "01",
    title: "Đo",
    body:
      "Trên mỗi máy khách có một chân gác nhỏ. Cứ vài giây nó ghi lại: CPU bao nhiêu, đĩa còn bao nhiêu, " +
      "dịch vụ nào còn sống, cổng nào còn mở.",
    who: "Nó chỉ ghi số. Không kết luận.",
  },
  {
    n: "02",
    title: "So với thói quen",
    body:
      "Omni nhớ máy này bình thường trông ra sao, rồi so số mới với thói quen đó. Trong ngưỡng thì bỏ qua, " +
      "không làm phiền ai.",
    who: "Đây là chỗ Omni tự học, không ai đặt ngưỡng tay.",
  },
  {
    n: "03",
    title: "Điều tra",
    body:
      "Lệch ngưỡng thì mới coi là sự cố. Lúc đó Omni tra sổ tay cũ trước; không có việc giống thì mới hỏi " +
      "mô hình ngôn ngữ.",
    who: "Tra sổ trước, hỏi máy sau — để đỡ tốn và đỡ bịa.",
  },
  {
    n: "04",
    title: "Báo người",
    body:
      "Kết luận được ký, đóng dấu vào sổ không sửa được, rồi mới gửi thẻ sự cố qua Telegram cho người đọc.",
    who: "Ký sổ hỏng thì dừng, không gửi gì cả.",
  },
];

type DotKind = "on" | "off" | "q";

interface Place {
  title: string;
  caption: string;
  items: { kind: DotKind; lead?: string; text: string }[];
}

const PLACES: Place[] = [
  {
    title: "Máy của khách",
    caption: "3 máy ảo, nằm ngoài cụm",
    items: [
      { kind: "on", lead: "cust-edge", text: "— web nginx, chia sẻ file. Chân gác đang chạy." },
      { kind: "on", lead: "cust-app", text: "— ứng dụng cổng 8080. Chân gác đang chạy." },
      { kind: "on", lead: "cust-db", text: "— MySQL và Redis. Chân gác đang chạy." },
    ],
  },
  {
    title: "Bộ não Omni",
    caption: "trong cụm Kubernetes",
    items: [
      { kind: "on", lead: "Cửa nhận", text: "— chỗ duy nhất máy khách gửi số vào." },
      { kind: "on", lead: "Bộ nghĩ", text: "— làm gần như mọi việc: so thói quen, điều tra, ký sổ, gửi thẻ." },
      { kind: "on", lead: "Bộ nhận việc mới", text: "— lo phần tiếp nhận khách hàng mới." },
      { kind: "on", text: "Kho: hàng đợi, bộ nhớ nhanh, cơ sở dữ liệu." },
      { kind: "off", lead: "3 dịch vụ tắt hẳn", text: "— cố ý, việc của chúng đã gộp vào Bộ nghĩ." },
      { kind: "q", lead: "nginx-test", text: "— đang chạy mà không tài liệu nào nhắc tới." },
    ],
  },
  {
    title: "Nơi người nhìn thấy",
    caption: "trong nhà và trên Internet",
    items: [
      { kind: "on", lead: "Telegram", text: "— thẻ sự cố tiếng Việt." },
      { kind: "on", lead: "Cổng nội bộ", text: "— bản cho nhà cung cấp và bản cho khách thuê." },
      { kind: "on", lead: "app.omnisre.xyz", text: "— công khai trên Internet, chỉ mở cho một email." },
      { kind: "q", text: "Bản công khai chỉ có phía nhà cung cấp. Phía khách thuê chưa có." },
    ],
  },
];

interface Beat {
  n: number;
  title: string;
  body: string;
  /** Có thì đây là chặng CÓ THỂ CHẶN — mọi thứ dừng lại nếu không qua. */
  gate?: string;
}

const BEATS: Beat[] = [
  {
    n: 1,
    title: "Chân gác gửi số về",
    body: "Ví dụ: CPU của cust-db vừa đo được 94%. Chỉ là con số, chưa mang nhãn gì.",
  },
  {
    n: 2,
    title: "Cửa nhận phân loại",
    body: "Số đo thường thì đi đường «ghi nhớ». Chỉ thứ đã mang nhãn sự cố mới đi đường điều tra.",
    gate:
      "Chặng chặn — số đo thường KHÔNG gọi mô hình ngôn ngữ. Đây là lý do hệ thống không đốt tiền mỗi giây.",
  },
  {
    n: 3,
    title: "So với thói quen",
    body:
      "Nếu 94% là bình thường với máy này thì dừng ở đây, ghi nhớ rồi thôi. Nếu lệch hẳn thì mới được nâng " +
      "lên thành sự cố và quay ngược vào đường điều tra.",
    gate: "Chặng chặn — đại đa số số đo dừng tại đây.",
  },
  {
    n: 4,
    title: "Gom bằng chứng và tra sổ tay",
    body:
      "Gom các dấu hiệu liên quan lại thành một vụ, rồi tìm trong sổ tay xem đã gặp vụ giống chưa. " +
      "Giống đủ nhiều thì dùng lại lời giải cũ, bỏ qua bước hỏi máy.",
  },
  {
    n: 5,
    title: "Hỏi mô hình ngôn ngữ",
    body:
      "Chỉ khi sổ tay không có. Trả lời theo khuôn cố định: chuyện gì, ai bị ảnh hưởng, vì sao, làm gì tiếp, " +
      "và dự báo diễn biến.",
  },
  {
    n: 6,
    title: "Ký sổ không sửa được",
    body: "Kết luận được băm và ký, nối vào chuỗi sổ cũ. Mục đích: sau này không ai sửa lịch sử được.",
    gate:
      "Chặng chặn nghiêm nhất — ký hỏng là dừng toàn bộ, không gửi thẻ, không làm gì. " +
      "Thà im còn hơn làm mà không có dấu vết.",
  },
  {
    n: 7,
    title: "Gửi thẻ cho người",
    body: "Thẻ tiếng Việt qua Telegram. Người đọc, quyết định đồng ý hay không.",
  },
  {
    n: 8,
    title: "Ra tay sửa",
    body: "Đây là chặng duy nhất Omni động vào hệ thống thật.",
    gate: "Chặng chặn — hiện đang khoá, chặng này không chạy.",
  },
];

export function PlainView() {
  return (
    <div className="arch-panel" data-testid="arch-plain">
      <section className="arch-sec">
        <div className="arch-sec-head">
          <span className="arch-eyebrow">Bản vẽ 2 · không cần biết kỹ thuật</span>
          <h2 className="arch-h2">Omni làm gì — bốn việc, theo thứ tự</h2>
          <p className="arch-p">
            Điểm mấu chốt: <strong>nhân viên gác ở máy khách chỉ đo, không phán.</strong> Việc kết luận
            «cái này bất thường» luôn nằm ở Omni.
          </p>
        </div>
        <div className="arch-cards">
          {STEPS.map((s) => (
            <article className="arch-card" key={s.n}>
              <span className="arch-card-n">{s.n}</span>
              <h3 className="arch-h3">{s.title}</h3>
              <p className="arch-p">{s.body}</p>
              <span className="arch-card-foot">{s.who}</span>
            </article>
          ))}
        </div>
      </section>

      <section className="arch-sec">
        <div className="arch-sec-head">
          <span className="arch-eyebrow">Cái gì chạy ở đâu</span>
          <h2 className="arch-h2">Ba chỗ, đã gõ lệnh kiểm từng chỗ</h2>
          <p className="arch-p">Không đọc tài liệu rồi chép lại — mỗi dòng dưới đây đến từ một lệnh đã chạy.</p>
        </div>
        <div className="arch-places">
          {PLACES.map((p) => (
            <article className="arch-place" key={p.title}>
              <header className="arch-place-cap">
                <h3 className="arch-h3">{p.title}</h3>
                <small>{p.caption}</small>
              </header>
              <ul>
                {p.items.map((it) => (
                  <li key={(it.lead ?? "") + it.text}>
                    <span className={`arch-dot ${it.kind}`} aria-hidden />
                    <span>
                      {it.lead && <b>{it.lead} </b>}
                      {it.text}
                    </span>
                  </li>
                ))}
              </ul>
            </article>
          ))}
        </div>
      </section>

      <section className="arch-sec">
        <div className="arch-sec-head">
          <span className="arch-eyebrow">Một sự cố đi qua đâu</span>
          <h2 className="arch-h2">Tám chặng, đọc từ trên xuống</h2>
          <p className="arch-p">
            Ô vàng là <strong>chặng có thể chặn</strong> — nơi mọi thứ dừng lại nếu không qua.
          </p>
        </div>
        <div className="arch-track">
          {BEATS.map((b) => (
            <div className={`arch-beat${b.gate ? " gate" : ""}`} key={b.n}>
              <span className="arch-pin">{b.n}</span>
              <div className="arch-beat-body">
                <h3 className="arch-h3">{b.title}</h3>
                <p className="arch-p">{b.body}</p>
                {b.gate && <p className="arch-gatenote">{b.gate}</p>}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="arch-sec">
        <div className="arch-sec-head">
          <span className="arch-eyebrow">Trạng thái đo được</span>
          <h2 className="arch-h2">Omni đang ở chế độ tập sự</h2>
          <p className="arch-p">
            Ba con số này đọc trực tiếp từ tiến trình đang chạy, không phải từ file cấu hình.
          </p>
        </div>
        <div className="arch-stats">
          <div className="arch-stat">
            <span className="arch-stat-k">công tắc tự ra tay</span>
            <span className="arch-stat-v stop">TẮT</span>
            <span className="arch-stat-m">Omni không được phép tự sửa bất cứ thứ gì.</span>
          </div>
          <div className="arch-stat">
            <span className="arch-stat-k">cấp độ tự chủ</span>
            <span className="arch-stat-v warn">shadow</span>
            <span className="arch-stat-m">Chỉ quan sát và nói. Nấc thấp nhất trong ba nấc.</span>
          </div>
          <div className="arch-stat">
            <span className="arch-stat-k">chân gác trên máy khách</span>
            <span className="arch-stat-v ok">3 / 3</span>
            <span className="arch-stat-m">Cả ba máy đều đang gửi số về đều đặn.</span>
          </div>
        </div>
        <p className="arch-note calm">
          <span className="arch-note-t">Nghĩa là gì</span>
          Omni <strong>nhìn được và nói được, nhưng chưa làm được.</strong> Đúng với ý «nhân viên đang thử
          việc»: hết thử việc mới nâng lên shadow → minimal → autonomous. Nhưng chặng 8 chưa từng chạy thật
          lần nào, nên chưa có bằng chứng nó hoạt động đúng khi được mở.
        </p>
      </section>

      <footer className="arch-foot">
        Dựng từ: liệt kê tiến trình trong cụm, đọc biến môi trường trong tiến trình đang chạy, hỏi bộ nhớ
        nhanh về cấp độ tự chủ, và kiểm chân gác trên cả ba máy khách. Đo ngày {MEASURED_AT}. Bản kỹ thuật
        đầy đủ nằm ở tab «Sơ đồ kỹ thuật».
      </footer>
    </div>
  );
}
