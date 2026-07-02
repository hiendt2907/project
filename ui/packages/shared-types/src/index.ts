// Kiểu chung hai portal. KHÔNG chứa chính sách authz/disclosure của riêng portal nào —
// chỉ hình dạng dữ liệu do backend AOIP (nguồn sự thật) trả về.

export type PortalKind = "provider" | "tenant";

/** Kết quả /me của Provider Portal (backend đã enforce quyền server-side). */
export interface ProviderIdentity {
  subject: string;
  kind: "provider";
  roles: string[];
  permissions: string[];
}

/** Kết quả /me của Tenant Portal. active_tenant do backend suy từ membership. */
export interface TenantIdentity {
  subject: string;
  kind: "tenant";
  active_tenant: string;
  roles: string[];
  permissions: string[];
  memberships: Record<string, string>;
}

export type Identity = ProviderIdentity | TenantIdentity;

/** Trạng thái phiên mà server component phân giải được từ backend /me. */
export type SessionState =
  | { status: "authenticated"; identity: Identity }
  | { status: "unauthenticated" } // 401
  | { status: "forbidden" } // 403 — đăng nhập nhưng sai vai trò/loại portal
  | { status: "expired" }; // lỗi khác / phiên hỏng

// ── Provider Control Tower ───────────────────────────────────────────────────
// Mỗi metric HOẶC có value (nguồn thật) HOẶC available=false + reason (khe hở nguồn
// nêu rõ). Không bao giờ số giả. Khớp backend src/aoip/console/overview.py.
export type Metric<T> =
  | { available: true; value: T }
  | { available: false; reason: string };

export interface ComponentHealth {
  name: string;
  status: string; // ok | down | unavailable
  detail?: string;
}

export interface RecentActivity {
  tenant: string;
  correlation_id: string;
  incident_id: string;
  event: string;
  reason: string;
  timestamp: number;
}

export interface ProviderOverview {
  generated_at: number;
  tenants: Metric<{ total: number; active: number; suspended: number }>;
  tenants_onboarding: Metric<number>;
  agents: Metric<{ online: number; offline: number; total: number }>;
  missions: Metric<number>;
  active_incidents: Metric<number>;
  pending_approvals: Metric<number>;
  pending_questions: Metric<number>;
  reconcile_required: Metric<number>;
  component_health: Metric<ComponentHealth[]>;
  recent_activity: Metric<RecentActivity[]>;
}

export interface ProviderAgentLastCheck {
  probe: string;
  ts: number;
  result: string;
  summary: string;
}

export interface ProviderAgent {
  agent_id: string;
  tenant_id: string;
  hostname: string;
  status: "online" | "stale" | "offline" | string;
  online: boolean;
  age_seconds: number;
  last_seen: number;
  version: string;
  platform: string;
  capabilities: string[];
  discovery_enabled: boolean;
  evidence_count: number;
  last_discovery_result: ProviderAgentLastCheck | null;
  command_state: string;
  pending_commands: number;
}

export interface ProviderAgentsResponse {
  generated_at: number;
  summary: { total: number; online: number; stale: number; offline: number };
  agents: ProviderAgent[];
}

export interface ProviderTwinFact {
  subject: string;
  predicate: string;
  object: string;
  confidence: number;
  provenance: string[];
  observation_time: number;
  verified_time: number;
  freshness_seconds: number;
}

export interface ProviderCompetencyProjection {
  entity_type: string;
  entity_id: string;
  coverage: { coverage_pct: number; state_counts: Record<string, number> };
  critical_unknowns: string[];
  contradicted_facets: string[];
}

export interface ProviderTenantUnderstanding {
  tenant_id: string;
  twin: {
    revision: number;
    entity_count: number;
    fact_count: number;
    relationship_count: number;
    unknown_edge_targets: string[];
  };
  entities: string[];
  relationships: ProviderTwinFact[];
  facts: ProviderTwinFact[];
  contradictions: Record<string, unknown>[];
  contradiction_count: number;
  unknowns: Record<string, unknown>[];
  unknown_count: number;
  questions: Record<string, unknown>[];
  question_count: number;
  competency: ProviderCompetencyProjection[];
}

export interface ProviderUnderstandingResponse {
  generated_at: number;
  tenant_count: number;
  tenants: ProviderTenantUnderstanding[];
}

export interface ProviderQuestion {
  question_id: string;
  tenant_id: string;
  entity_type: string;
  entity_id: string;
  facet: string;
  text: string;
  context_summary: string;
  known_evidence: string[];
  status: string;
  target_role: string;
  can_create_claim: boolean;
  created_at: number;
  answer_id?: string;
}

export interface ProviderHumanInboxTenant {
  tenant_id: string;
  unknown_count: number;
  question_count: number;
  pending_questions: number;
  questions: ProviderQuestion[];
}

export interface ProviderHumanInboxResponse {
  generated_at: number;
  summary: { tenants: number; unknowns: number; pending_questions: number };
  tenants: ProviderHumanInboxTenant[];
}

/** Một mục điều hướng Provider. `implemented=false` → route đánh dấu unavailable, KHÔNG giả. */
export interface NavItem {
  label: string;
  href: string;
  implemented: boolean;
  /** Sub-slice sẽ triển khai (hiển thị lý do khe hở khi chưa làm). */
  slice?: string;
}
