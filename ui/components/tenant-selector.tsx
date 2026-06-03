"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname, useSearchParams } from "next/navigation";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function TenantSelector() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [tenants, setTenants] = useState<string[]>([]);
  const current = searchParams.get("tenant") ?? "default";

  useEffect(() => {
    fetch("/api/tenants")
      .then((r) => r.json())
      .then((d: { tenants: string[] }) => setTenants(d.tenants ?? ["default"]))
      .catch(() => setTenants(["default"]));
  }, []);

  function onChange(value: string | null) {
    if (!value) return;
    const params = new URLSearchParams(searchParams.toString());
    params.set("tenant", value);
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }

  if (tenants.length === 0) return null;

  return (
    <div className="flex items-center gap-2">
      <span className="text-[10px] uppercase tracking-widest text-zinc-500">Tenant</span>
      <Select value={current} onValueChange={onChange}>
        <SelectTrigger className="h-7 w-36 border-zinc-800 bg-zinc-900 text-xs text-zinc-200 focus:ring-cyan-500/50">
          <SelectValue />
        </SelectTrigger>
        <SelectContent className="border-zinc-800 bg-zinc-900 text-zinc-200">
          {tenants.map((t) => (
            <SelectItem key={t} value={t} className="text-xs focus:bg-zinc-800">
              {t}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
