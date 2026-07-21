import { Card, KeyVal } from "@aoip/ui-kit";
import type { GatewaySectionResult } from "@/lib/gateway";
import type { DiagnosisTurn, TraceAdvisory, TraceSession } from "@/lib/trace-diagnosis";

// Diễn giải "vì sao AI kết luận vậy" cho một lượt xử lý — phần mở rộng của
// trang chi tiết pipeline. Ưu tiên phiên chẩn đoán đa lượt (session) khi có;
// nếu không, dùng báo cáo một lượt (advisory); không có cả hai là bình
// thường (chỉ sự cố mức critical/high mới chạy vòng chẩn đoán sâu) — hiển
// thị giải thích, KHÔNG phải lỗi. Xem docstring src/gateway/routes/trace.py.

function pctVI(v: number): string {
  return `${Math.round((v || 0) * 100)}%`;
}

function cmdStatusPill(c: { blocked: boolean; rc: number }): { cls: string; label: string } {
  if (c.blocked) return { cls: "failed", label: "bị chặn" };
  if (c.rc === 0) return { cls: "online", label: "rc=0" };
  return { cls: "active", label: `rc=${c.rc}` };
}

function TurnDetails({ turn }: { turn: DiagnosisTurn }) {
  return (
    <details className="aoip-diag-turn" open={turn.turn === 1} data-testid={`diag-turn-${turn.turn}`}>
      <summary className="aoip-diag-turn-summary">
        <span>Lượt {turn.turn}</span>
        <span className="aoip-muted">tin cậy {pctVI(turn.confidence)}</span>
        {turn.diagnosis_complete_claimed && <span className="aoip-pill online">đã kết luận</span>}
      </summary>
      <div className="aoip-diag-turn-body">
        {turn.hypothesis && (
          <>
            <div className="aoip-k">Giả thuyết</div>
            <p className="aoip-v">{turn.hypothesis}</p>
          </>
        )}
        {turn.reasoning && (
          <>
            <div className="aoip-k">Lập luận</div>
            <p className="aoip-v aoip-diag-pre">{turn.reasoning}</p>
          </>
        )}
        {turn.command_results.length > 0 && (
          <>
            <div className="aoip-k">Lệnh đã chạy ({turn.command_results.length})</div>
            <ul className="aoip-diag-cmd-list">
              {turn.command_results.map((c) => {
                const pill = cmdStatusPill(c);
                return (
                  <li key={c.cmd_id} className="aoip-diag-cmd">
                    <div className="aoip-diag-cmd-head">
                      <span className={`aoip-pill ${pill.cls}`}>{pill.label}</span>
                      <code>{c.command_str}</code>
                    </div>
                    {c.purpose && <div className="aoip-muted">{c.purpose}</div>}
                    {c.preview.head.length > 0 && (
                      <pre className="aoip-diag-pre">
                        {c.preview.head.join("\n")}
                        {c.preview.truncated ? `\n… (${c.preview.total_lines} dòng, đã rút gọn)` : ""}
                      </pre>
                    )}
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </div>
    </details>
  );
}

function AdvisoryDetails({ adv }: { adv: NonNullable<TraceAdvisory["advisory"]> }) {
  return (
    <>
      <KeyVal label="Kết luận">{adv.verdict || "—"}</KeyVal>
      <KeyVal label="Độ tin cậy">{adv.confidence || "—"}</KeyVal>
      {adv.root_cause && <KeyVal label="Nguyên nhân gốc">{adv.root_cause}</KeyVal>}
      {adv.affected_workload && <KeyVal label="Ảnh hưởng">{adv.affected_workload}</KeyVal>}

      {adv.verification_steps?.length > 0 && (
        <>
          <div className="aoip-k">Bước kiểm chứng</div>
          <ol className="aoip-v aoip-diag-steps">
            {adv.verification_steps.map((s) => (
              <li key={s.order}>
                <code>{s.command}</code>
                <div className="aoip-muted">{s.rationale} → kỳ vọng: {s.expected_output}</div>
              </li>
            ))}
          </ol>
        </>
      )}

      {adv.impact_chain && adv.impact_chain.length > 0 && (
        <>
          <div className="aoip-k">Chuỗi tác động</div>
          <ul className="aoip-v aoip-diag-chain">
            {adv.impact_chain.map((c, i) => (
              <li key={i}>
                {c.cause} → {c.mechanism} → {c.effect}
                <span className="aoip-muted"> [{c.evidence_lane} · {c.confidence}]</span>
              </li>
            ))}
          </ul>
        </>
      )}

      {adv.proposed_remediation?.length > 0 && (
        <>
          <div className="aoip-k">Đề xuất khắc phục</div>
          <ol className="aoip-v">
            {adv.proposed_remediation.map((r) => (
              <li key={r.order}>
                {r.action} {r.approval_required && <span className="aoip-pill active">cần duyệt</span>}
              </li>
            ))}
          </ol>
        </>
      )}

      {adv.forecast?.forecasts && adv.forecast.forecasts.length > 0 && (
        <>
          <div className="aoip-k">Dự báo ({adv.forecast.method})</div>
          <div className="aoip-chip-row">
            {adv.forecast.forecasts.map((f) => (
              <span key={f.timeframe} className="aoip-chip">{f.timeframe}: {f.severity}</span>
            ))}
          </div>
        </>
      )}
    </>
  );
}

export function TraceDiagnosisSection({
  session,
  advisory,
}: {
  session: GatewaySectionResult<TraceSession>;
  advisory: GatewaySectionResult<TraceAdvisory>;
}) {
  if (session.data?.found) {
    const s = session.data;
    return (
      <>
        <Card>
          <div className="aoip-k">Kết luận chẩn đoán (đa lượt)</div>
          <KeyVal label="Số lượt phân tích">{s.total_turns}</KeyVal>
          <KeyVal label="Độ tin cậy cuối">{pctVI(s.final.confidence)}</KeyVal>
          {s.final.root_cause && <KeyVal label="Nguyên nhân gốc">{s.final.root_cause}</KeyVal>}
          {s.final.blast_radius && <KeyVal label="Phạm vi ảnh hưởng">{s.final.blast_radius}</KeyVal>}
          {s.final.remediation_steps.length > 0 && (
            <>
              <div className="aoip-k">Khắc phục đề xuất</div>
              <ol className="aoip-v">
                {s.final.remediation_steps.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
            </>
          )}
          {s.degraded && (
            <div className="aoip-muted" data-testid="diag-degraded">
              ⚠ Suy giảm trong quá trình chẩn đoán: {s.degraded_reason}
            </div>
          )}
        </Card>
        <Card>
          <div className="aoip-k">Diễn biến từng lượt phân tích</div>
          <div data-testid="diag-turns">
            {s.turns.map((t) => (
              <TurnDetails key={t.turn} turn={t} />
            ))}
          </div>
        </Card>
      </>
    );
  }

  if (advisory.data?.found && advisory.data.advisory) {
    return (
      <Card>
        <div className="aoip-k">Báo cáo chẩn đoán (một lượt)</div>
        <div data-testid="diag-advisory">
          <AdvisoryDetails adv={advisory.data.advisory} />
        </div>
      </Card>
    );
  }

  if (session.error && advisory.error) {
    return (
      <Card error>
        <div className="aoip-k err">Không tải được chi tiết chẩn đoán</div>
        <div className="aoip-state" data-testid="diag-error">
          Nguồn dữ liệu ({session.error}). Thử tải lại trang.
        </div>
      </Card>
    );
  }

  return (
    <Card>
      <div className="aoip-k">Chi tiết chẩn đoán</div>
      <div className="aoip-state" data-testid="diag-empty">
        Chưa có phiên chẩn đoán đa lượt hay báo cáo một lượt nào được lưu cho lượt xử lý này.
        Vòng chẩn đoán sâu chỉ chạy cho sự cố mức critical/high; sự cố mức thấp hơn có thể
        không có báo cáo chi tiết — đây là trạng thái bình thường, không phải lỗi.
      </div>
    </Card>
  );
}
