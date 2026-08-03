import { MermaidBlock } from "@/components/mermaid-diagram";
import { TECHNICAL_DIAGRAMS, DRIFT, OUT_OF_SCOPE, MEASURED_AT } from "./diagrams";

/** Bản vẽ 1 — 6 sơ đồ Mermaid kỹ thuật, mỗi sơ đồ kèm bảng nguồn xác minh.
 *  Mermaid render CLIENT-SIDE (MermaidBlock, dynamic import) — không rasterize
 *  server-side, đúng tiền lệ trang /understanding. */
export function TechnicalView() {
  return (
    <div className="arch-panel" data-testid="arch-technical">
      <section className="arch-sec">
        <div className="arch-sec-head">
          <span className="arch-eyebrow">Bản vẽ 1 · cho kỹ sư</span>
          <h2 className="arch-h2">Sáu sơ đồ, mỗi sơ đồ trả lời đúng một câu hỏi</h2>
          <p className="arch-p">
            Mọi node và mũi tên dưới đây được xác minh bằng code hoặc cluster thật vào{" "}
            <strong>{MEASURED_AT}</strong>, không chép lại tài liệu cũ. Mỗi sơ đồ mở được bảng{" "}
            <em>nguồn xác minh</em> — lệnh hoặc dòng mã đã dùng để chứng minh từng node.
          </p>
        </div>
        <nav className="arch-toc" aria-label="Mục lục sơ đồ">
          {TECHNICAL_DIAGRAMS.map((d, i) => (
            <a key={d.id} href={`#arch-${d.id}`}>
              <span>{String(i + 1).padStart(2, "0")}</span>
              {d.title}
            </a>
          ))}
        </nav>
      </section>

      {TECHNICAL_DIAGRAMS.map((d, i) => (
        <section className="arch-plate" id={`arch-${d.id}`} key={d.id} data-testid={`arch-diagram-${d.id}`}>
          <header className="arch-plate-head">
            <span className="arch-plate-n">{String(i + 1).padStart(2, "0")}</span>
            <div className="arch-plate-titles">
              <h2 className="arch-h3">{d.title}</h2>
              <span className="arch-plate-q">{d.question}</span>
            </div>
          </header>
          <div className="arch-plate-body">
            <div className="arch-canvas">
              <MermaidBlock source={d.mermaid} />
            </div>
            {d.note && <p className="arch-note">{d.note}</p>}
            <details className="arch-src">
              <summary>Nguồn xác minh · {d.sources.length} mục</summary>
              <div className="aoip-table-wrap">
                <table className="aoip-table">
                  <thead>
                    <tr>
                      <th>Node / bước</th>
                      <th>Xác minh bằng</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.sources.map((s) => (
                      <tr key={s.node}>
                        <td>{s.node}</td>
                        <td>
                          <code className="arch-code">{s.by}</code>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </div>
        </section>
      ))}

      <section className="arch-sec">
        <div className="arch-sec-head">
          <span className="arch-eyebrow">Kết quả phụ</span>
          <h2 className="arch-h2">Drift phát hiện được</h2>
          <p className="arch-p">
            Mâu thuẫn giữa tài liệu và thực tế đo được ngày {MEASURED_AT}.{" "}
            <strong>Chưa sửa gì</strong> — liệt kê để quyết định.
          </p>
        </div>
        <div className="aoip-table-wrap">
          <table className="aoip-table" data-testid="arch-drift-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Phát hiện</th>
                <th>Bằng chứng</th>
                <th>Mức</th>
              </tr>
            </thead>
            <tbody>
              {DRIFT.map((r) => (
                <tr key={r.n}>
                  <td>{r.n}</td>
                  <td>{r.finding}</td>
                  <td>
                    <code className="arch-code">{r.evidence}</code>
                  </td>
                  <td>
                    <span className={`arch-tag ${r.level === "khớp" ? "k" : r.level === "cần chốt" ? "q" : ""}`}>
                      {r.level}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="arch-sec">
        <div className="arch-sec-head">
          <span className="arch-eyebrow">Ranh giới</span>
          <h2 className="arch-h2">Cái sơ đồ này KHÔNG trả lời được</h2>
          <p className="arch-p">Ghi rõ để không nhầm phạm vi.</p>
        </div>
        <div className="arch-cards">
          {OUT_OF_SCOPE.map((o) => (
            <article className="arch-card" key={o.title}>
              <h3 className="arch-h3">{o.title}</h3>
              <p className="arch-p">{o.body}</p>
            </article>
          ))}
        </div>
      </section>

      <footer className="arch-foot">
        Bản kỹ thuật đầy đủ, có neo tới từng dòng mã: <code className="arch-code">docs/architecture/SYSTEM_DIAGRAMS.md</code>.
        Lệnh đã chạy để dựng: <code className="arch-code">kubectl get deploy,sts,cronjob,ingress -n multi-agent</code> ·{" "}
        <code className="arch-code">kubectl exec -n multi-agent deploy/omni-fullstack -- printenv | grep OMNI_</code> ·{" "}
        <code className="arch-code">redis-cli GET omni:cfg:tier:default</code> ·{" "}
        <code className="arch-code">orb list &amp;&amp; orb -m &lt;vm&gt; systemctl is-active omni-remote-agent.service</code>.
      </footer>
    </div>
  );
}
