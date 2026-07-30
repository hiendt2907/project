"use client";

// Nút "Test lại" — đẩy một sự cố mẫu qua đường remote-agent THẬT (RAG→LLM→CRAT→
// Telegram) rồi mở thẳng trang chi tiết từng bước của lượt vừa chạy. Không mock:
// cùng pipeline sự cố thật đi qua. KHÔNG dừng service vật lý trên VM (pod không có
// quyền chạm máy khách) — muốn tác động VM thật dùng scripts/diag-test-vm.sh trên host.
import { useState } from "react";
import { useRouter } from "next/navigation";

interface Scenario {
  key: string;
  label: string;
  hint: string;
}

const SCENARIOS: Scenario[] = [
  { key: "service", label: "Dịch vụ dừng", hint: "systemd unit chuyển inactive → domain=service" },
  { key: "network", label: "Mất cổng lắng nghe", hint: "cổng TCP đóng đột ngột → domain=network" },
  { key: "disk", label: "Đĩa gần đầy", hint: "disk cao → domain=storage (hàng rào tĩnh)" },
  { key: "cpu", label: "Tải CPU cao", hint: "cpu vượt baseline → domain=os_host" },
];

export function TestPanel() {
  const router = useRouter();
  const [running, setRunning] = useState<string | null>(null);
  const [msg, setMsg] = useState<string>("");
  const [lastTrace, setLastTrace] = useState<string>("");

  async function run(scenario: string) {
    setRunning(scenario);
    setMsg("Đang đẩy sự cố mẫu qua pipeline thật…");
    setLastTrace("");
    try {
      const res = await fetch("/api/gateway/diagnostics/test", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ scenario, tenant_id: "default" }),
      });
      const data = await res.json();
      if (!res.ok) {
        setMsg(`Lỗi: ${data?.error ?? res.status}`);
        return;
      }
      const traceId = String(data?.trace_id ?? "");
      if (traceId) {
        setLastTrace(traceId);
        setMsg("Đã đẩy. Vòng chẩn đoán đang chạy (RAG→LLM→CRAT). Chờ ~15–30s rồi mở chi tiết.");
      } else {
        setMsg("Đã gửi nhưng gateway không trả trace_id — kiểm tra danh sách bên dưới.");
      }
    } catch {
      setMsg("Không gọi được máy chủ.");
    } finally {
      setRunning(null);
    }
  }

  return (
    <div className="aoip-diagtest">
      <div className="aoip-diagtest-grid">
        {SCENARIOS.map((s) => (
          <button
            key={s.key}
            className="aoip-diagtest-btn"
            disabled={running !== null}
            onClick={() => run(s.key)}
            data-testid={`diagtest-${s.key}`}
          >
            <span className="aoip-diagtest-label">
              {running === s.key ? "Đang chạy…" : s.label}
            </span>
            <span className="aoip-diagtest-hint">{s.hint}</span>
          </button>
        ))}
      </div>
      {msg && (
        <div className="aoip-diagtest-msg" data-testid="diagtest-msg">
          {msg}
          {lastTrace && (
            <>
              {" "}
              <button
                className="aoip-diagtest-open"
                onClick={() => router.push(`/pipeline/${encodeURIComponent(lastTrace)}`)}
              >
                Mở chi tiết từng bước →
              </button>
            </>
          )}
        </div>
      )}
      <p className="aoip-diagtest-note">
        Nút này chạy đúng pipeline sự cố thật (chẩn đoán đa lượt, RAG, kiểm chứng bằng
        lệnh read-only, ghi CRAT), chỉ khác một điều: nó không dừng dịch vụ vật lý trên
        máy khách. Muốn thử tác động VM thật (dừng nginx rồi khôi phục), chạy{" "}
        <code>scripts/diag-test-vm.sh</code> trên máy chủ lab.
      </p>
    </div>
  );
}
