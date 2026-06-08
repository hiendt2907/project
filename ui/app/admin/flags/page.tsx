"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { RuntimeFlagsPanel } from "@/components/admin/RuntimeFlagsPanel";

function Inner() {
  const tenant = useSearchParams().get("tenant") ?? "default";
  return (
    <div className="p-4">
      <RuntimeFlagsPanel tenant={tenant} />
    </div>
  );
}

export default function FlagsPage() {
  return (
    <Suspense>
      <Inner />
    </Suspense>
  );
}
