"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { RiskClassMatrixPanel } from "@/components/admin/RiskClassMatrixPanel";

function Inner() {
  const tenant = useSearchParams().get("tenant") ?? "default";
  return (
    <div className="p-4">
      <RiskClassMatrixPanel tenant={tenant} />
    </div>
  );
}

export default function RiskClassPage() {
  return (
    <Suspense>
      <Inner />
    </Suspense>
  );
}
