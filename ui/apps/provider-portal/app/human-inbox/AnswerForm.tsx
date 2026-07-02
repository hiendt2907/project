"use client";

import { useState } from "react";

export function AnswerForm({ tenantId, questionId }: { tenantId: string; questionId: string }) {
  const [value, setValue] = useState("");
  const [state, setState] = useState<"idle" | "saving" | "saved" | "error">("idle");

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!value.trim()) return;
    setState("saving");
    const res = await fetch(`/api/provider/v1/questions/${encodeURIComponent(tenantId)}/${encodeURIComponent(questionId)}/answer`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ value: value.trim() }),
    });
    if (!res.ok) {
      setState("error");
      return;
    }
    setState("saved");
  }

  return (
    <form className="aoip-answer" onSubmit={submit}>
      <input
        className="aoip-input"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Nhập câu trả lời"
        disabled={state === "saving" || state === "saved"}
      />
      <button className="aoip-btn" type="submit" disabled={state === "saving" || state === "saved"}>
        {state === "saving" ? "Đang lưu" : state === "saved" ? "Đã lưu" : "Lưu Claim"}
      </button>
      {state === "error" ? <span className="aoip-err">Lưu thất bại</span> : null}
    </form>
  );
}
