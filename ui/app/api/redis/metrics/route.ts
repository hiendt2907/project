import { NextResponse } from "next/server";

function rand(min: number, max: number) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

const COLLECTIONS = [
  { name: "k8s_expert", vectors: 18420 },
  { name: "sop_runbooks", vectors: 5234 },
  { name: "incident_history", vectors: 9871 },
  { name: "errors", vectors: 1203 },
  { name: "semcache", vectors: 442 },
];

const HISTORY = Array.from({ length: 24 }, (_, i) => ({
  hour: `${String(i).padStart(2, "0")}:00`,
  hits: rand(40, 180),
  misses: rand(5, 40),
  latencyMs: rand(8, 45),
}));

export async function GET() {
  await new Promise((r) => setTimeout(r, 60));
  const totalVectors = COLLECTIONS.reduce((acc, c) => acc + c.vectors, 0);
  const recentHits = HISTORY.slice(-6).reduce((acc, h) => acc + h.hits, 0);
  const recentMisses = HISTORY.slice(-6).reduce((acc, h) => acc + h.misses, 0);
  const hitRatio = Math.round((recentHits / (recentHits + recentMisses)) * 100);

  return NextResponse.json({
    totalVectors,
    hitRatio,
    missRatio: 100 - hitRatio,
    memoryUsed: "1.28 GiB",
    memoryMax: "2.00 GiB",
    memoryPct: 64,
    connectedClients: rand(3, 12),
    opsPerSec: rand(80, 340),
    collections: COLLECTIONS,
    history: HISTORY,
  });
}
