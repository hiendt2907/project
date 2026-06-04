import type { NextRequest } from "next/server";

// Real SSE proxy for pipeline stage events.
//
// Pipes the gateway's `/trace/stream` (text/event-stream over omni:trace:events)
// straight through to the browser. Bearer auth is attached server-side so the
// client EventSource (which cannot set Authorization headers) stays unauthenticated.
//
// Node runtime + force-dynamic + no-transform/X-Accel-Buffering keep Traefik and
// Next from buffering the long-lived stream. Falls back to a keep-alive comment
// stream when the gateway is not configured (UI then relies on polling).

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

const SSE_HEADERS = {
  "Content-Type": "text/event-stream; charset=utf-8",
  "Cache-Control": "no-cache, no-transform",
  Connection: "keep-alive",
  "X-Accel-Buffering": "no",
} as const;

export async function GET(request: NextRequest): Promise<Response> {
  // No gateway → emit a heartbeat-only stream so EventSource stays open; the
  // page keeps its polling fallback and the dashboard still works on mocks.
  if (!GATEWAY_URL) {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(": no-gateway heartbeat\n\n"));
        const timer = setInterval(() => {
          try {
            controller.enqueue(encoder.encode(": ping\n\n"));
          } catch {
            clearInterval(timer);
          }
        }, 15000);
        request.signal.addEventListener("abort", () => {
          clearInterval(timer);
          try {
            controller.close();
          } catch {
            /* already closed */
          }
        });
      },
    });
    return new Response(stream, { headers: SSE_HEADERS });
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${GATEWAY_URL}/trace/stream`, {
      headers: GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {},
      cache: "no-store",
      signal: request.signal,
    });
  } catch {
    return new Response(": upstream-unreachable\n\n", { headers: SSE_HEADERS });
  }

  if (!upstream.ok || !upstream.body) {
    return new Response(`: upstream-status-${upstream.status}\n\n`, { headers: SSE_HEADERS });
  }

  // Pass the upstream byte stream straight through — it is already SSE-framed.
  return new Response(upstream.body, { headers: SSE_HEADERS });
}
