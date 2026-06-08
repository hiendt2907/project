import { NextResponse } from "next/server";

// Shared Omni Gateway proxy helpers for Admin autonomy routes.
// Every config write hits the gateway with Bearer master key (server-side only).

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

export function authHeaders(): HeadersInit {
  return GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {};
}

export function gatewayConfigured(): boolean {
  return Boolean(GATEWAY_URL);
}

export function gatewayError(detail: string, status = 502) {
  return NextResponse.json({ source: "error", error: detail }, { status });
}

/** Proxy a GET to the gateway and echo its JSON (status preserved on error). */
export async function proxyGet(path: string) {
  if (!GATEWAY_URL) return gatewayError("OMNI_GATEWAY_URL not configured");
  try {
    const res = await fetch(`${GATEWAY_URL}${path}`, {
      headers: authHeaders(),
      cache: "no-store",
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return NextResponse.json({ source: "error", ...data }, { status: res.status });
    return NextResponse.json({ source: "gateway", ...data });
  } catch {
    return gatewayError(`gateway ${path} unreachable`);
  }
}

/** Proxy a body-bearing method (POST/DELETE) to the gateway. */
export async function proxyBody(path: string, method: "POST" | "DELETE", body?: unknown) {
  if (!GATEWAY_URL) return gatewayError("OMNI_GATEWAY_URL not configured");
  try {
    const res = await fetch(`${GATEWAY_URL}${path}`, {
      method,
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return NextResponse.json({ source: "error", ...data }, { status: res.status });
    return NextResponse.json({ source: "gateway", ...data });
  } catch {
    return gatewayError(`gateway ${path} ${method} unreachable`);
  }
}
