import type { BackendConfig } from "@aoip/api-client";

// Cấu hình môi trường RIÊNG của Provider Portal.
export const PROVIDER_API_BASE = "/api/provider/v1";

export const backendConfig: BackendConfig = {
  ssrBaseUrl: process.env.AOIP_BACKEND_URL ?? "http://localhost:8081",
  apiBase: PROVIDER_API_BASE,
};

export const TELEMETRY_NS = "provider-portal";
