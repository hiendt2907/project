import { Card } from "@aoip/ui-kit";
import type { SystemTwinResponse } from "@/lib/system-twin";

const NOISE = /^(systemd|dbus|rpc|nfs|cron|rsyslog|agetty|console|getty|user@|\(sd-pam\)|orbstack|omni-|conn-keepalive|traffic-loop|fsidd|blkmapd|sleep|ps|ss)/i;
const EDGE = /^(nginx|haproxy|traefik|envoy|httpd|apache2|caddy|kong)$/i;
const DATA = /^(mariadb|mariadbd|mysql|mysqld|postgres|postgresql|redis|redis-server|mongodb|mongod|memcached|valkey)$/i;

export function SystemTwinPanel({
  tenantId,
  result,
}: {
  tenantId: string;
  result: { data: SystemTwinResponse | null; error: string | null };
}) {
  if (!result.data) {
    return (
      <Card>
        <div className="aoip-k">System Twin · {tenantId}</div>
        <div className="aoip-state">Chưa đọc được read-model{result.error ? ` · ${result.error}` : ""}</div>
      </Card>
    );
  }

  const twin = result.data;
  return (
    <section className="aoip-system-twin-panel">
      <div className="aoip-section-head">
        <div>
          <div className="aoip-k">System Twin · {tenantId}</div>
          <div className="aoip-muted">Revision {twin.revision} · read-model từ discovery thật</div>
        </div>
        <span className={`aoip-pill ${twin.summary.contradictions ? "stale" : "online"}`}>
          {twin.summary.contradictions ? "Cần đối soát" : "Sạch"}
        </span>
      </div>
      <div className="aoip-twin-grid">
        <TwinMetric label="Hosts" value={twin.summary.hosts} />
        <TwinMetric label="Services" value={twin.summary.services} />
        <TwinMetric label="Edges" value={twin.summary.edges} />
        <TwinMetric label="Unknowns" value={twin.summary.unknowns} />
        <TwinMetric label="Contradictions" value={twin.summary.contradictions} />
      </div>
      <OperationalTopology hosts={twin.operational_hosts ?? []} />
      <ObservedApiSequence hosts={twin.operational_hosts ?? []} sequence={twin.api_sequence} />
    </section>
  );
}

function TwinMetric({ label, value }: { label: string; value: number }) {
  return <div className="aoip-twin-metric"><span>{label}</span><strong>{value}</strong></div>;
}

function OperationalTopology({ hosts }: { hosts: SystemTwinResponse["operational_hosts"] }) {
  const visible = hosts.map((host) => ({ ...host, services: host.services.filter((service) => !NOISE.test(service.name)) }));
  const tierFor = (host: typeof visible[number]) => {
    if (host.services.some((service) => EDGE.test(service.name))) return "edge";
    if (host.services.some((service) => DATA.test(service.name))) return "data";
    return "app";
  };
  const tierMeta = [
    { key: "edge", label: "EDGE / INGRESS" },
    { key: "app", label: "APPLICATION" },
    { key: "data", label: "DATA" },
  ] as const;
  const nodes = visible.filter((host) => host.services.length || host.connections.length);
  const grouped = tierMeta.map((tier) => ({ ...tier, items: nodes.filter((host) => tierFor(host) === tier.key) }));
  const nodeWidth = 214;
  const rowHeight = 148;
  const graphWidth = Math.max(900, ...grouped.map((tier) => tier.items.length * 250 + 80));
  const graphHeight = rowHeight * grouped.length + 50;
  const positions = new Map(nodes.map((host) => {
    const tier = tierFor(host);
    const row = grouped.findIndex((item) => item.key === tier);
    const column = grouped[row].items.findIndex((item) => item.host === host.host);
    return [host.host, { x: 40 + column * 250, y: 42 + row * rowHeight, row }] as const;
  }));
  const edges = nodes.flatMap((host) => host.connections.flatMap((connection) => {
    const source = positions.get(host.host);
    const target = positions.get(connection.target);
    if (!source || !target || host.host === connection.target) return [];
    const [a, b] = [host.host, connection.target].sort();
    return [{ key: `${a}:${b}`, from: host.host, to: connection.target, confidence: connection.confidence }];
  })).filter((edge, index, all) => all.findIndex((item) => item.key === edge.key) === index);
  return (
    <div className="aoip-operational-topology" data-testid="operational-topology">
      <div className="aoip-k">System topology · operational view</div>
      <div className="aoip-muted aoip-topology-note">Customer system only · nodes and arrows are derived from observed hosts, services, ports and connections. Platform noise stays in audit evidence.</div>
      <div className="aoip-topology-viewport">
        <div className="aoip-topology-graph" style={{ width: graphWidth, height: graphHeight }}>
          <svg className="aoip-topology-edges" viewBox={`0 0 ${graphWidth} ${graphHeight}`} aria-label="Observed customer system connections">
            <defs><marker id="aoip-topology-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" /></marker></defs>
            {edges.map((edge) => {
              const from = positions.get(edge.from)!;
              const to = positions.get(edge.to)!;
              const forward = from.row <= to.row;
              const start = forward ? { x: from.x + nodeWidth / 2, y: from.y + 76 } : { x: from.x + nodeWidth, y: from.y + 38 };
              const end = forward ? { x: to.x + nodeWidth / 2, y: to.y } : { x: to.x, y: to.y + 38 };
              const midY = (start.y + end.y) / 2;
              return <g key={edge.key}><path className="aoip-topology-edge" d={`M ${start.x} ${start.y} C ${start.x} ${midY}, ${end.x} ${midY}, ${end.x} ${end.y}`} markerEnd="url(#aoip-topology-arrow)" /><text x={(start.x + end.x) / 2} y={midY - 5}>{Math.round(edge.confidence * 100)}% observed</text></g>;
            })}
          </svg>
          {grouped.map((tier) => <div className={`aoip-topology-row ${tier.key}`} key={tier.key} style={{ top: 42 + tierMeta.findIndex((item) => item.key === tier.key) * rowHeight }}><span>{tier.label}</span></div>)}
          {nodes.map((host) => {
            const position = positions.get(host.host)!;
            return <div className={`aoip-topology-graph-node ${tierFor(host)}`} key={host.host} style={{ left: position.x, top: position.y }}>
              <strong>{host.host.replace(/^host:/, "")}</strong>
              <div className="aoip-muted">{host.services.flatMap((service) => service.ports).length ? `ports ${host.services.flatMap((service) => service.ports).join(", ")}` : "service observed"}</div>
              <div className="aoip-topology-services">{host.services.map((service) => <span className="aoip-topology-service" key={service.name}>{service.name}{service.ports.length ? ` · ${service.ports.join(", ")}` : ""}</span>)}</div>
            </div>;
          })}
          {!nodes.length ? <div className="aoip-state aoip-topology-empty">Chưa đủ evidence để vẽ topology.</div> : null}
        </div>
      </div>
    </div>
  );
}

function ObservedApiSequence({ hosts, sequence }: { hosts: SystemTwinResponse["operational_hosts"]; sequence: SystemTwinResponse["api_sequence"] }) {
  const visible = hosts.map((host) => ({ ...host, services: host.services.filter((s) => !NOISE.test(s.name)) }));
  const edge = visible.find((host) => host.services.some((s) => EDGE.test(s.name)));
  const app = visible.find((host) => host.services.some((s) => !EDGE.test(s.name) && !DATA.test(s.name)));
  const data = visible.find((host) => host.services.some((s) => DATA.test(s.name)));
  const connected = (from: typeof edge, to: typeof app) => Boolean(
    from && to && from.connections.some((connection) => connection.target === to.host),
  );
  const edgeToApp = connected(edge, app);
  const appToData = connected(app, data);
  const steps = [
    edge ? `${edge.host.replace(/^host:/, "")} · ${edge.services.find((s) => EDGE.test(s.name))?.name}` : null,
    app && (!edge || edgeToApp) ? `${app.host.replace(/^host:/, "")} · ${app.services.find((s) => !EDGE.test(s.name) && !DATA.test(s.name))?.name}` : null,
    data && (!app || appToData) ? `${data.host.replace(/^host:/, "")} · ${data.services.filter((s) => DATA.test(s.name)).map((s) => s.name).join(" + ")}` : null,
  ].filter(Boolean) as string[];
  return (
    <div className="aoip-api-sequence" data-testid="observed-api-sequence">
      <div className="aoip-section-head">
        <div>
          <div className="aoip-k">API sequence · customer system</div>
          <div className="aoip-muted">Chỉ đánh dấu HTTP khi access-log metadata đã quan sát method + route + status. Không truyền query, header, body hay token.</div>
        </div>
        <span className={`aoip-pill ${sequence.status === "runtime_verified" ? "online" : sequence.status === "contract_observed" ? "stale" : "stale"}`}>
          {sequence.status === "runtime_verified" ? "Runtime verified" : sequence.status === "contract_observed" ? "Contract only" : sequence.status === "missing_contract" ? "Contract required" : "Network dependency"}
        </span>
      </div>
      {sequence.status === "runtime_verified" || sequence.status === "contract_observed" ? (
        <div className="aoip-api-observations">
          {sequence.interactions.map((item) => (
            <div className="aoip-api-observation" key={`${item.source_host}:${item.method}:${item.route}:${item.status_class}`}>
              <span className="aoip-sequence-index">{item.count}</span>
              <div><code>{item.method} {item.route}</code>{item.operation_id ? <div className="aoip-muted">{item.operation_id}</div> : null}</div>
              <span className="aoip-muted">{item.status_class} · {item.source_host.replace(/^host:/, "")}{item.target_host ? ` → ${item.target_host}` : " · upstream chưa được log"}{item.runtime_observed ? " · runtime hit" : " · contract only"}</span>
            </div>
          ))}
        </div>
      ) : sequence.status === "network_only" && steps.length ? (
        <div className="aoip-sequence-track">
          <div className="aoip-sequence-step"><span className="aoip-sequence-index">0</span><strong>Client / caller</strong></div>
          {steps.map((step, index) => <div className="aoip-sequence-step" key={step}><span className="aoip-sequence-index">{index + 1}</span><strong>{step}</strong></div>)}
        </div>
      ) : <div className="aoip-state">Chưa đủ evidence để vẽ API path.</div>}
      {sequence.status !== "runtime_verified" ? <div className="aoip-muted aoip-sequence-note">{sequence.unknown_reasons.join(" ")} {sequence.status === "network_only" ? "Connection scan chỉ chứng minh dependency TCP, chưa chứng minh request order." : "Sequence chỉ là contract shape; cần access log để xác nhận runtime."}</div> : null}
    </div>
  );
}
