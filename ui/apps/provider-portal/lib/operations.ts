// Việc cần xử lý (reconcile-required / chưa báo cáo) — console /operations.
// Nguồn: src/aoip/console/app.py (provider app, Trace Spine).
import { backendGet } from "@aoip/api-client";
import { backendConfig } from "@/lib/config";

export interface OperationItem {
  tenant: string;
  correlation_id: string;
  phase: string;
  reconcile_required: boolean;
}

export interface TenantItem {
  tenant: string;
  tenant_id?: string;
  display_name?: string;
  status?: string;
  incidents: number;
}

export interface EnvironmentItem {
  environment_id: string;
  tenant_id: string;
  display_name: string;
  environment_type: string;
  status: string;
}

export interface TenantPlan {
  plan_code: string;
  agent_limit: number;
  autonomy_ceiling: string;
  retention_days: number;
  support_tier: string;
  enabled: boolean;
}

export type OperationsResult =
  | { status: "ok"; data: { operations: OperationItem[] } }
  | { status: "error"; code: number };

export type TenantsResult =
  | { status: "ok"; data: { tenants: TenantItem[] } }
  | { status: "error"; code: number };

export async function fetchOperations(cookieHeader: string): Promise<OperationsResult> {
  let resp: Response;
  try {
    resp = await backendGet(backendConfig, "/operations", cookieHeader);
  } catch {
    return { status: "error", code: 0 };
  }
  if (!resp.ok) return { status: "error", code: resp.status };
  return { status: "ok", data: (await resp.json()) as { operations: OperationItem[] } };
}

export async function fetchTenants(cookieHeader: string): Promise<TenantsResult> {
  let resp: Response;
  try {
    resp = await backendGet(backendConfig, "/tenants", cookieHeader);
  } catch {
    return { status: "error", code: 0 };
  }
  if (!resp.ok) return { status: "error", code: resp.status };
  const raw = (await resp.json()) as { tenants: Array<Record<string, unknown>> };
  return {
    status: "ok",
    data: {
      tenants: raw.tenants.map((t) => ({
        tenant: String(t.tenant ?? t.tenant_id ?? ""),
        tenant_id: typeof t.tenant_id === "string" ? t.tenant_id : undefined,
        display_name: typeof t.display_name === "string" ? t.display_name : undefined,
        status: typeof t.status === "string" ? t.status : undefined,
        incidents: Number(t.incidents ?? 0),
      })),
    },
  };
}

export type EnvironmentsResult =
  | { status: "ok"; data: { environments: EnvironmentItem[] } }
  | { status: "error"; code: number };

export async function fetchEnvironments(
  cookieHeader: string, tenantId: string,
): Promise<EnvironmentsResult> {
  let resp: Response;
  try {
    resp = await backendGet(
      backendConfig,
      `/tenants/${encodeURIComponent(tenantId)}/environments`,
      cookieHeader,
    );
  } catch {
    return { status: "error", code: 0 };
  }
  if (!resp.ok) return { status: "error", code: resp.status };
  return {
    status: "ok",
    data: (await resp.json()) as { environments: EnvironmentItem[] },
  };
}

export async function fetchTenantPlan(
  cookieHeader: string, tenantId: string,
): Promise<{ status: "ok"; data: TenantPlan } | { status: "error"; code: number }> {
  let resp: Response;
  try { resp = await backendGet(backendConfig, `/tenants/${encodeURIComponent(tenantId)}/plan`, cookieHeader); }
  catch { return { status: "error", code: 0 }; }
  if (!resp.ok) return { status: "error", code: resp.status };
  return { status: "ok", data: (await resp.json()) as TenantPlan };
}
