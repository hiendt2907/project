export type OmniUiRealm = "portal" | "ops" | "local";

export const PORTAL_HOST = "portal.ai-agent.local";
export const OMNI_HOST = "omni.ai-agent.local";

export function realmFromHost(host: string | null): OmniUiRealm {
  const h = (host ?? "").split(":")[0];
  if (h === PORTAL_HOST) return "portal";
  if (h === OMNI_HOST) return "ops";
  return "local";
}
