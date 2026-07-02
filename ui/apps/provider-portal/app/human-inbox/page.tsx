import { headers } from "next/headers";
import { Card, MetricStat } from "@aoip/ui-kit";
import type { ProviderQuestion } from "@aoip/shared-types";
import { fetchHumanInbox } from "@/lib/human-inbox";
import { AnswerForm } from "./AnswerForm";

export default async function ProviderHumanInboxPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const result = await fetchHumanInbox(cookieHeader);

  if (result.status === "error") {
    return (
      <Card error>
        <div className="aoip-k err">Không tải được Human Inbox</div>
        <div className="aoip-state" data-testid="human-inbox-error">
          Backend trả mã {result.code || "không phản hồi"}. Thử tải lại trang.
        </div>
      </Card>
    );
  }

  const { summary, tenants } = result.data;
  return (
    <>
      <div className="aoip-k">Human Inbox</div>
      <div className="aoip-grid" data-testid="human-inbox-summary">
        <MetricStat label="Tenants with gaps" value={summary.tenants} />
        <MetricStat label="Unknown records" value={summary.unknowns} />
        <MetricStat label="Pending questions" value={summary.pending_questions} />
      </div>

      {tenants.length === 0 ? (
        <Card><div className="aoip-state">Không có Unknown/Question nào.</div></Card>
      ) : tenants.map((tenant) => (
        <Card key={tenant.tenant_id}>
          <div className="aoip-k">{tenant.tenant_id}</div>
          <div className="aoip-muted">
            {tenant.unknown_count} unknowns · {tenant.pending_questions} pending questions
          </div>
          {tenant.questions.length === 0 ? (
            <div className="aoip-state">Chưa có câu hỏi.</div>
          ) : (
            tenant.questions.slice(0, 40).map((q) => <QuestionCard key={q.question_id} q={q} />)
          )}
        </Card>
      ))}
    </>
  );
}

function QuestionCard({ q }: { q: ProviderQuestion }) {
  const pending = q.status === "PENDING";
  return (
    <div
      className="aoip-question"
      data-testid={`question-${q.question_id}`}
      data-claimable={q.can_create_claim ? "true" : "false"}
    >
      <div className="aoip-row">
        <span>{q.entity_id} · {q.facet}</span>
        <span className="aoip-chip-row">
          {q.can_create_claim ? <span className="aoip-pill active">Claim</span> : null}
          <span className={`aoip-pill ${pending ? "active" : "idle"}`}>{q.status}</span>
        </span>
      </div>
      <div>{q.text}</div>
      <div className="aoip-muted">{q.context_summary}</div>
      {q.known_evidence?.length ? (
        <div className="aoip-muted">Evidence: {q.known_evidence.join(" · ")}</div>
      ) : null}
      {pending ? (
        <AnswerForm tenantId={q.tenant_id} questionId={q.question_id} />
      ) : (
        <div className="aoip-muted">answer_id={q.answer_id ?? "n/a"}</div>
      )}
    </div>
  );
}
