// Shared lane design tokens — single source for the 4 diagnostic lanes.
// Previously duplicated (LANE_LABEL / LANE_BORDER / LANE_TEXT / LANE_DOT) across
// admin and operator pages.

export const LANES = ["SYS_RESOURCE", "SYS_HARD_FAIL", "APP_HTTP", "SIEM_SECURITY"] as const;
export type Lane = (typeof LANES)[number];

export const LANE_LABEL: Record<string, string> = {
  SYS_RESOURCE: "RESOURCE",
  SYS_HARD_FAIL: "HARDFAIL",
  APP_HTTP: "HTTP",
  SIEM_SECURITY: "SIEM",
};

export const LANE_DESC: Record<Lane, string> = {
  SYS_RESOURCE: "Time-series 3σ baseline — CPU/mem anomaly (z>3.0)",
  SYS_HARD_FAIL: "OS/state machine — systemd, disk, NFS, MySQL hard failures",
  APP_HTTP: "HTTP status classes — 5xx / 429 / 499 / auth surge",
  SIEM_SECURITY: "Smart-SIEM — DDoS, malware, exfil, lateral movement",
};

export const LANE_TEXT: Record<string, string> = {
  SYS_RESOURCE: "text-cyan-400",
  SYS_HARD_FAIL: "text-rose-400",
  APP_HTTP: "text-amber-400",
  SIEM_SECURITY: "text-violet-400",
};

export const LANE_BORDER: Record<string, string> = {
  SYS_RESOURCE: "border-l-cyan-500",
  SYS_HARD_FAIL: "border-l-rose-500",
  APP_HTTP: "border-l-amber-500",
  SIEM_SECURITY: "border-l-violet-500",
};

export const LANE_DOT: Record<string, string> = {
  SYS_RESOURCE: "bg-cyan-400",
  SYS_HARD_FAIL: "bg-rose-400",
  APP_HTTP: "bg-amber-400",
  SIEM_SECURITY: "bg-violet-400",
};
