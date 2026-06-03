<!-- Generated: 2026-05-22 | Files scanned: 65 TS/TSX | Token estimate: ~750 -->

# Frontend — Omni SRE UI

## Framework
Next.js App Router (`ui/`) — deployed as `omni-ui` pod, accessible via `omni.ai-agent.local`.
Auth: NextAuth (JWT). Realm detection: portal / ops / local (via hostname).

## Page Tree

```
ui/app/
├── page.tsx                  Landing / redirect
├── layout.tsx                Root layout (dark theme, amber accents, monospace font)
├── globals.css               Global styles
├── admin/page.tsx            Admin Dashboard — SRE engineer view
├── operator/page.tsx         Operator Console — on-call engineer view (lane-colored)
├── kpi/page.tsx              KPI Dashboard — acceptance/MTTD/MTTR (read-only)
├── siem/page.tsx             SIEM incident browser (kill-chain + category charts)
├── incidents/                Incident list + detail pages
├── playbooks/                Playbook CRUD + step viewer
├── ledger/page.tsx           Error ledger (level/worker/timestamp filters)
├── workers/page.tsx          Worker pod health grid + heartbeat
├── remote-agents/page.tsx    Remote agent registry + metrics + probe logs
├── login/page.tsx            NextAuth credentials sign-in
├── onboarding/page.tsx       First-run setup wizard
├── deploy/page.tsx           Deployment management
└── config/autonomy/          Autonomy policy configuration
```

## API Proxy Routes (ui/app/api/)

```
/api/alerts          → POST  /webhook/prometheus (gateway)
/api/auth            → NextAuth session
/api/config          → GET/POST gateway config
/api/crat            → GET /crat/export → CRAT audit blocks
/api/deploy          → POST gateway deploy actions
/api/hitl            → GET/POST /hitl/pending
/api/incidents       → GET /siem/overview + KPI incidents
/api/kpi             → GET /kpi/summary (mock fallback when gateway down)
/api/ledger          → GET /compliance/export
/api/onboarding      → onboarding state
/api/playbooks       → GET /playbooks
/api/redis           → Redis key browser
/api/remote-agents   → GET /agents/remote (with metrics + logs sub-route)
/api/siem            → SIEM incidents
/api/workers         → GET /healthz workers
```

## Key Pages

### Admin Dashboard (`admin/page.tsx`)
- System Health Bar — pod grid (6-col mini-cards)
- KPI Live — acceptance rate / MTTD / MTTR by lane
- CRAT Audit Chain — recent blocks with hash display
- Active Traces — in-flight trace_ids
- Alert Injection Form — POST /webhook/prometheus
- Tenant Management — multi-tenant key config
- Style: dark luxury, amber (#F59E0B) accents, monospace

### Operator Console (`operator/page.tsx`)
- Diagnostic Lanes — left-border lane colors (LANE_BORDER map)
- Advisory Panel — expandable verification steps + remediation
- HITL Queue — countdown timer, approve/reject actions
- Telegram Status — bot health indicator
- Style: dark, split-panel (incident list | detail)

### KPI Dashboard (`kpi/page.tsx`)
- 4 stat cards: total / accepted / rejected / false_positive
- Pie charts: acceptance & false-positive rates (recharts)
- Lane resolution bar chart: MTTD/MTTR per lane
- Data: GET /api/kpi → /kpi/summary (mock fallback)

### SIEM Page (`siem/page.tsx`)
- Kill chain stage progression (DDoS/malware/data_exfil/etc.)
- Category distribution pie chart
- Incident severity breakdown
- Data: GET /api/siem → SiemOpsResponse

### Remote Agents (`remote-agents/page.tsx`)
- Agent registry with health status + metrics bars
- Tabbed interface: system / database / k8s / logs collectors
- Probe log expansion per agent
- Data: GET /api/remote-agents (with /logs sub-route)

### Workers (`workers/page.tsx`)
- Worker pod status grid
- STATUS_BORDER/BG/LABEL/COLOR maps per status
- Heartbeat timestamp display

## Components & Lib

```
ui/components/sidebar.tsx     Navigation: ops/portal/local realm switching (NavItem type)
ui/lib/omni-ui-realm.ts       realmFromHost(): OmniUiRealm type (portal/ops/local)
ui/middleware.ts              NextAuth middleware: realm-based route protection
ui/mocks/admin-mock.ts        Static mock — admin page fallback (MOCK_POD_HEALTH, MOCK_KPI)
ui/mocks/operator-mock.ts     Static mock — operator page fallback
ui/types/                     Shared TypeScript interfaces
```

## Lane Color Map

```
SYS_RESOURCE  → amber   (#F59E0B)
SYS_HARD_FAIL → red     (#EF4444)
APP_HTTP      → blue    (#3B82F6)
SIEM_SECURITY → purple  (#8B5CF6)
```

## State Management
- Server state: direct `fetch()` to `/api/*` proxy routes with mock fallbacks
- No global client state store
- Live sections (health, KPI) use `setInterval` polling
