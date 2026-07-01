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

/** Một mục điều hướng Provider. `implemented=false` → route đánh dấu unavailable, KHÔNG giả. */
export interface NavItem {
  label: string;
  href: string;
  implemented: boolean;
  /** Sub-slice sẽ triển khai (hiển thị lý do khe hở khi chưa làm). */
  slice?: string;
}
