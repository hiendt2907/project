import { NextResponse } from "next/server";

const pods = [
  { name: "omni-prober-0", role: "prober", status: "Running", restarts: 0, cpu: "42m", memory: "128Mi", age: "2d", ready: true },
  { name: "omni-analyst-6d9f4b-xkz9q", role: "analyst", status: "Running", restarts: 1, cpu: "310m", memory: "512Mi", age: "2d", ready: true },
  { name: "omni-core-5c7d8b-pqr4m", role: "core", status: "Running", restarts: 0, cpu: "88m", memory: "256Mi", age: "2d", ready: true },
  { name: "omni-executor-7f6b9c-mnv2k", role: "executor", status: "Running", restarts: 0, cpu: "55m", memory: "192Mi", age: "1d", ready: true },
  { name: "omni-gateway-deployment-abc12", role: "gateway", status: "Running", restarts: 0, cpu: "28m", memory: "96Mi", age: "2d", ready: true },
  { name: "omni-siem-bridge-xyz99", role: "siem-bridge", status: "Running", restarts: 2, cpu: "120m", memory: "320Mi", age: "12h", ready: true },
  { name: "omni-hitl-dispatcher-7abc1", role: "hitl-dispatcher", status: "Running", restarts: 0, cpu: "15m", memory: "64Mi", age: "12h", ready: true },
  { name: "redis-0", role: "redis", status: "Running", restarts: 0, cpu: "220m", memory: "1.1Gi", age: "3d", ready: true },
];

export async function GET() {
  await new Promise((r) => setTimeout(r, 80));
  const running = pods.filter((p) => p.status === "Running").length;
  return NextResponse.json({ pods, summary: { total: pods.length, running, failed: 0 } });
}
