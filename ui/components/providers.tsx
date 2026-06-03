"use client";

import { createContext, useContext, type ReactNode } from "react";
import { SessionProvider } from "next-auth/react";
import type { OmniUiRealm } from "@/lib/omni-ui-realm";

const OmniUiRealmContext = createContext<OmniUiRealm>("local");

export function useOmniUiRealm(): OmniUiRealm {
  return useContext(OmniUiRealmContext);
}

export function Providers({
  children,
  realm = "local",
}: {
  children: ReactNode;
  realm?: OmniUiRealm;
}) {
  return (
    <OmniUiRealmContext.Provider value={realm}>
      <SessionProvider>{children}</SessionProvider>
    </OmniUiRealmContext.Provider>
  );
}
