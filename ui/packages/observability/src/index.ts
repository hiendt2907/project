// Telemetry tối thiểu, provider-neutral. Mỗi portal khởi tạo với namespace RIÊNG
// (telemetry tách biệt theo yêu cầu). Không log ra console ở production — chỉ đẩy vào
// sink có thể thay (mặc định no-op ở server). KHÔNG chứa chính sách portal.

export interface TelemetryEvent {
  ns: string; // namespace portal (vd "provider-portal" | "tenant-portal")
  name: string;
  ts: number;
  attrs?: Record<string, string | number | boolean>;
}

export type TelemetrySink = (e: TelemetryEvent) => void;

const noopSink: TelemetrySink = () => {};

export interface Telemetry {
  event(name: string, attrs?: TelemetryEvent["attrs"]): void;
}

/** Tạo telemetry cho 1 portal. Sink có thể nối OTLP/logging tuỳ deploy. */
export function createTelemetry(ns: string, sink: TelemetrySink = noopSink): Telemetry {
  return {
    event(name, attrs) {
      sink({ ns, name, ts: Date.now(), attrs });
    },
  };
}
