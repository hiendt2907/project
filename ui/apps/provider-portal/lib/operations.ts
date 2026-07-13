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
  incidents: number;
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
  return { status: "ok", data: (await resp.json()) as { tenants: TenantItem[] } };
}
