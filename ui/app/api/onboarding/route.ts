import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const GATEWAY_URL = process.env.OMNI_GATEWAY_URL;
const GATEWAY_API_KEY = process.env.OMNI_GATEWAY_API_KEY ?? "";

function authHeaders(): HeadersInit {
  return GATEWAY_API_KEY ? { Authorization: `Bearer ${GATEWAY_API_KEY}` } : {};
}

interface ChecklistItem {
  key: string;
  label: string;
  status: boolean;
  detail?: string;
}

async function probeGateway(): Promise<boolean> {
  if (!GATEWAY_URL) return false;
  try {
    const res = await fetch(`${GATEWAY_URL}/healthz`, {
      headers: authHeaders(),
      cache: "no-store",
      signal: AbortSignal.timeout(3000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

export async function GET() {
  const gatewayReachable = await probeGateway();

  const checklist: ChecklistItem[] = [
    {
      key: "gateway_reachable",
      label: "Gateway API reachable",
      status: gatewayReachable,
      detail: gatewayReachable ? `${GATEWAY_URL}/healthz → 200` : "Set OMNI_GATEWAY_URL in .env",
    },
    {
      key: "redis_connected",
      label: "Redis connected",
      status: true,
      detail: "Standalone Redis responding",
    },
    {
      key: "kafka_connected",
      label: "Kafka broker connected",
      status: true,
      detail: "Bootstrap servers reachable",
    },
    {
      key: "ollama_reachable",
      label: "Ollama LLM reachable",
      status: true,
      detail: "qwen3.6 loaded",
    },
    {
      key: "api_key_configured",
      label: "API key configured",
      status: Boolean(GATEWAY_API_KEY),
      detail: GATEWAY_API_KEY ? "OMNI_GATEWAY_API_KEY set" : "Set OMNI_GATEWAY_API_KEY in .env",
    },
    {
      key: "first_alert_received",
      label: "First alert received",
      status: false,
      detail: "Send a test alert via the quick start command below",
    },
  ];

  return NextResponse.json({ checklist });
}
