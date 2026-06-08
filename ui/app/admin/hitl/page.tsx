"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { HitlQueuePanel } from "@/components/admin/HitlQueuePanel";

function Inner() {
  const tenant = useSearchParams().get("tenant") ?? "default";
  return (
    <div className="p-4">
      <HitlQueuePanel tenant={tenant} />
    </div>
  );
}

export default function HitlPage() {
  return (
    <Suspense>
      <Inner />
    </Suspense>
  );
}
