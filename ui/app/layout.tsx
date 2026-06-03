import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { headers } from "next/headers";
import "./globals.css";
import { Providers } from "@/components/providers";
import { realmFromHost } from "@/lib/omni-ui-realm";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Omni SRE Dashboard",
  description: "Agentic SRE Platform Control Plane",
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const hdrs = await headers();
  const realm = realmFromHost(hdrs.get("host"));

  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="h-full bg-zinc-950 text-zinc-100">
        <Providers realm={realm}>{children}</Providers>
      </body>
    </html>
  );
}
