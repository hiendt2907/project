import type { BackendConfig } from "@aoip/api-client";

// Cấu hình môi trường RIÊNG của Tenant Portal.
export const TENANT_API_BASE = "/api/tenant/v1";

export const backendConfig: BackendConfig = {
  ssrBaseUrl: process.env.AOIP_BACKEND_URL ?? "http://localhost:8082",
  apiBase: TENANT_API_BASE,
};

export const TELEMETRY_NS = "tenant-portal";
