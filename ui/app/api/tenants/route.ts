import { NextResponse } from "next/server";

// GET /api/tenants → { tenants: string[] }
// Parses OMNI_TENANT_APIKEYS="default:key1,acme:key2,..." — never exposes keys.

export const dynamic = "force-dynamic";

function parseTenants(): string[] {
  const raw = process.env.OMNI_TENANT_APIKEYS ?? "";
  if (!raw.trim()) return ["default"];

  const ids = raw
    .split(",")
    .map((entry) => entry.trim().split(":")[0].trim())
    .filter((id) => id.length > 0 && /^[a-zA-Z0-9_-]+$/.test(id));

  return ids.length > 0 ? ids : ["default"];
}

export async function GET() {
  return NextResponse.json({ tenants: parseTenants() });
}
