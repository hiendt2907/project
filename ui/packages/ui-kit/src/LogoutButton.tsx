"use client";
// Nút đăng xuất — client component tối thiểu. POST tới logout path (same-origin) kèm
// header CSRF, rồi reload để server component render lại trạng thái đã đăng xuất.
import * as React from "react";

export function LogoutButton({ logoutPath }: { logoutPath: string }) {
  const [busy, setBusy] = React.useState(false);
  async function onClick() {
    setBusy(true);
    try {
      await fetch(logoutPath, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-AOIP-CSRF": "1" },
      });
    } finally {
      window.location.reload();
    }
  }
  return (
    <button className="aoip-btn" onClick={onClick} disabled={busy} data-testid="logout">
      {busy ? "Đang đăng xuất…" : "Đăng xuất"}
    </button>
  );
}
