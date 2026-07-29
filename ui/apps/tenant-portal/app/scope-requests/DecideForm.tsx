"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { TENANT_API_BASE } from "@/lib/config";
import styles from "../ledger.module.css";

/**
 * Đồng ý / từ chối một đơn, ngay trên trang.
 *
 * Gọi same-origin `${TENANT_API_BASE}/...` để cookie phiên HttpOnly được gửi kèm
 * — KHÔNG tự nghĩ ra cơ chế xác thực nào khác. Body cố ý không có `tenant_id`:
 * backend lấy tenant từ session, client không có cách can thiệp.
 */
export function DecideForm({ requestId }: { requestId: number }) {
  const router = useRouter();
  const [note, setNote] = useState("");
  const [state, setState] = useState<"idle" | "saving" | "done" | "error">("idle");
  const [message, setMessage] = useState("");

  async function decide(decision: "APPROVED" | "REJECTED") {
    if (state === "saving" || state === "done") return;
    setState("saving");
    try {
      const res = await fetch(
        `${TENANT_API_BASE}/competency/scope-requests/${requestId}/decide`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ decision, note: note.trim() }),
        },
      );
      if (!res.ok) {
        setState("error");
        setMessage(
          res.status === 403
            ? "Bạn không có quyền quyết định việc này."
            : res.status === 404
              ? "Đơn không còn ở trạng thái chờ (có thể ai đó vừa xử lý)."
              : `Không lưu được quyết định (${res.status}).`,
        );
        return;
      }
      setState("done");
      setMessage(decision === "APPROVED" ? "Đã đồng ý." : "Đã từ chối.");
      router.refresh();
    } catch {
      setState("error");
      setMessage("Không gửi được yêu cầu. Kiểm tra kết nối rồi thử lại.");
    }
  }

  const busy = state === "saving" || state === "done";

  return (
    <div className={styles.actions}>
      <input
        className="aoip-input"
        style={{ flex: "1 1 240px" }}
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Ghi chú cho quyết định (không bắt buộc)"
        disabled={busy}
        aria-label="Ghi chú cho quyết định"
      />
      <button className="aoip-btn" type="button" disabled={busy} onClick={() => decide("APPROVED")}>
        Đồng ý mở quyền
      </button>
      <button className="aoip-btn" type="button" disabled={busy} onClick={() => decide("REJECTED")}>
        Từ chối
      </button>
      {message ? (
        <span className={state === "error" ? "aoip-err" : "aoip-ok"}>{message}</span>
      ) : null}
      {state === "idle" ? (
        <span className="aoip-muted" style={{ fontSize: "var(--text-2xs)" }}>
          Từ chối sẽ khoá Omni xin lại loại việc này trong 14 ngày.
        </span>
      ) : null}
    </div>
  );
}
