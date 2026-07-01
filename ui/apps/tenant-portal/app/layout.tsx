import "@aoip/ui-kit/src/styles.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "AOIP · Your Operations",
  description: "AOIP Customer/Tenant Operations Portal",
};

// Route tree + navigation RIÊNG của Tenant Portal.
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="vi">
      <body data-kind="tenant">{children}</body>
    </html>
  );
}
