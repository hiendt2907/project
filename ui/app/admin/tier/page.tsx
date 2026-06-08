"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { TierControlPanel } from "@/components/admin/TierControlPanel";
import { AutonomyPanel } from "@/components/admin/AutonomyPanel";
import type { AutonomyPolicyResponse } from "@/app/api/config/autonomy/route";

function isErr(v: unknown): boolean {
  return (v as { source?: string } | null)?.source === "error";
}

// Local autonomy-policy fetch — the per-lane autonomy matrix lives behind /api/config/autonomy.
function useAutonomyPolicy() {
  const [autonomy, setAutonomy] = useState<AutonomyPolicyResponse | null>(null);
  const [error, setError] = useState(false);

  const reload = useCallback(async () => {
    try {
      const data = await fetch("/api/config/autonomy", { cache: "no-store" }).then((r) => r.json());
      if (isErr(data)) setError(true);
      else {
        setAutonomy(data as AutonomyPolicyResponse);
        setError(false);
      }
    } catch {
      setError(true);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { autonomy, error, reload };
}

function Inner() {
  const tenant = useSearchParams().get("tenant") ?? "default";
  const { autonomy, error, reload } = useAutonomyPolicy();

  return (
    <div className="p-4 space-y-5">
      <TierControlPanel tenant={tenant} />
      <AutonomyPanel autonomy={autonomy} error={error} onSaved={reload} />
    </div>
  );
}

export default function TierPage() {
  return (
    <Suspense>
      <Inner />
    </Suspense>
  );
}
