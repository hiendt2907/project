import { NextRequest, NextResponse } from "next/server";
import { getRedis } from "@/lib/redis";

export const dynamic = "force-dynamic";

type Playbook = {
  id: string;
  playbook_id?: string;
  name: string;
  description?: string;
  siem_categories?: string[];
  severity_filter?: string;
  actions?: string[];
  steps?: string[];
  auto_execute?: boolean;
  created_at?: string;
  updated_at?: string;
};

async function loadAll(): Promise<Playbook[]> {
  const redis = getRedis();
  const keys = await redis.keys("pb:*");
  if (keys.length === 0) return [];
  const out: Playbook[] = [];
  for (const k of keys) {
    try {
      const raw = (await redis.call("JSON.GET", k)) as string | null;
      if (!raw) continue;
      const obj = JSON.parse(raw);
      const id = obj.playbook_id || obj.id || k.replace(/^pb:/, "");
      out.push({ id, ...obj });
    } catch (err) {
      console.error(`[playbooks] failed to parse ${k}`, err);
    }
  }
  return out;
}

export async function GET(req: NextRequest) {
  const url = new URL(req.url);
  const cat = url.searchParams.get("category");
  const sev = url.searchParams.get("severity");

  let result = await loadAll();
  console.log(`[playbooks] redis fetch -> ${result.length} keys; sample=${result.map(p => p.name).slice(0, 5).join("|")}`);

  if (cat) result = result.filter((p) => (p.siem_categories || []).includes(cat));
  if (sev) result = result.filter((p) => p.severity_filter === sev);
  return NextResponse.json(result);
}

export async function POST(req: NextRequest) {
  const body = await req.json();
  const id = body.id || body.playbook_id || `pb-${Date.now()}`;
  const now = new Date().toISOString();
  const pb: Playbook = { id, created_at: now, updated_at: now, ...body };
  const redis = getRedis();
  await redis.call("JSON.SET", `pb:${id}`, "$", JSON.stringify(pb));
  return NextResponse.json(pb, { status: 201 });
}
