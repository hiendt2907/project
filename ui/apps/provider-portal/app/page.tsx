import { headers } from "next/headers";
import { Card, MetricStat, Unavailable } from "@aoip/ui-kit";
import type {
  ComponentHealth, Metric, RecentActivity,
} from "@aoip/shared-types";
import { fetchOverview } from "@/lib/overview";

// Provider Control Tower — số THẬT từ backend /overview (Trace Spine + agent registry + PG +
// liveness). Metric thiếu nguồn → <Unavailable reason/> (không số giả). Layout đã gate auth;
// nếu vẫn lỗi (đua phiên), hiển thị trạng thái lỗi rõ ràng.
export default async function ProviderOverviewPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const result = await fetchOverview(cookieHeader);

  if (result.status === "error") {
    return (
      <Card error>
        <div className="aoip-k err">Không tải được Overview</div>
        <div className="aoip-state" data-testid="overview-error">
          Backend trả mã {result.code || "không phản hồi"}. Thử tải lại trang.
        </div>
      </Card>
    );
  }

  const ov = result.overview;
  return (
    <>
      <div className="aoip-k">Provider Control Tower</div>
      <div className="aoip-grid" data-testid="overview-grid">
        <Stat label="Tenants" m={ov.tenants} render={(v) => v.total}
          hint={statTenantsHint(ov.tenants)} />
        <Stat label="Đang onboarding" m={ov.tenants_onboarding} render={(v) => v} />
        <Stat label="Agents online" m={ov.agents} render={(v) => v.online}
          hint={statAgentsHint(ov.agents)} />
        <Stat label="Agents offline" m={ov.agents} render={(v) => v.offline} />
        <Stat label="Missions" m={ov.missions} render={(v) => v} />
        <Stat label="Sự cố đang mở" m={ov.active_incidents} render={(v) => v} />
        <Stat label="Chờ phê duyệt" m={ov.pending_approvals} render={(v) => v} />
        <Stat label="Câu hỏi chờ" m={ov.pending_questions} render={(v) => v} />
        <Stat label="Cần reconcile" m={ov.reconcile_required} render={(v) => v} />
      </div>

      <ComponentHealthCard m={ov.component_health} />
      <RecentActivityCard m={ov.recent_activity} />

      <div className="aoip-muted">
        Số liệu suy từ nguồn runtime thật (Trace Spine · agent registry · Postgres · liveness).
        Mục «chưa khả dụng» nêu rõ khe hở nguồn — không hiển thị dữ liệu giả.
      </div>
    </>
  );
}

function Stat<T>({ label, m, render, hint }: {
  label: string; m: Metric<T>; render: (v: T) => React.ReactNode; hint?: string;
}) {
  if (!m.available) {
    return <MetricStat label={label} value={<Unavailable reason={m.reason} />} hint={m.reason} />;
  }
  return <MetricStat label={label} value={render(m.value)} hint={hint} />;
}

function statTenantsHint(m: Metric<{ active: number; suspended: number }>): string | undefined {
  return m.available ? `active ${m.value.active} · suspended ${m.value.suspended}` : undefined;
}

function statAgentsHint(m: Metric<{ total: number }>): string | undefined {
  return m.available ? `tổng ${m.value.total}` : undefined;
}

function ComponentHealthCard({ m }: { m: Metric<ComponentHealth[]> }) {
  return (
    <Card>
      <div className="aoip-k">AOIP component health</div>
      {m.available ? (
        m.value.map((c) => (
          <div className="aoip-row" key={c.name} data-testid={`health-${c.name}`}>
            <span><span className={`aoip-dot ${c.status}`} />{c.name}</span>
            <span className="aoip-muted">{c.detail ? `${c.status} · ${c.detail}` : c.status}</span>
          </div>
        ))
      ) : (
        <div className="aoip-state"><Unavailable reason={m.reason} /></div>
      )}
    </Card>
  );
}

function RecentActivityCard({ m }: { m: Metric<RecentActivity[]> }) {
  return (
    <Card>
      <div className="aoip-k">Hoạt động gần đây (Trace Spine)</div>
      {!m.available ? (
        <div className="aoip-state"><Unavailable reason={m.reason} /></div>
      ) : m.value.length === 0 ? (
        <div className="aoip-state" data-testid="activity-empty">
          Chưa có hoạt động runtime nào được ghi nhận.
        </div>
      ) : (
        m.value.map((e) => (
          <div className="aoip-row" key={`${e.tenant}:${e.correlation_id}:${e.timestamp}`}>
            <span>{e.tenant} · {e.event}</span>
            <span className="aoip-muted">{e.reason || e.incident_id}</span>
          </div>
        ))
      )}
    </Card>
  );
}
