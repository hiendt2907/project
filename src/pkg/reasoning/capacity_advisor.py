"""Đề xuất capacity/scale từ xu hướng — trục S5 "nhìn trước" (G4).

Khác biệt giữa một cảnh báo ngưỡng và một senior SRE nằm ở câu "còn bao lâu nữa thì
vỡ", chứ không phải "hiện đang cao". Module này đọc chuỗi mẫu baseline
(`3sigma:remote:{tenant}:{host}:{metric}`, list Redis) và ước lượng xu hướng tuyến
tính + thời gian chạm ngưỡng.

INVARIANT AN TOÀN:
- Thuần tuý: không I/O, không Redis, không K8s. Dễ kiểm thử, không có tác dụng phụ.
- Kết quả CỐ Ý không mang `tool`/`args` và luôn `auto_execute=False`. Đây là văn bản
  đề xuất cho người đọc, KHÔNG phải lệnh chạy được — mọi mutation vẫn phải đi qua
  executor + tier gate như mọi hành động khác.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ACTION_SCALE_UP = "SCALE_UP"
ACTION_SCALE_DOWN = "SCALE_DOWN"
ACTION_HOLD = "HOLD"
ACTION_INVESTIGATE_LEAK = "INVESTIGATE_LEAK"
ACTION_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

MIN_SAMPLES = 20
DEFAULT_THRESHOLD = 90.0
DEFAULT_SCALE_DOWN_CEILING = 15.0
# Độ dốc dưới mức này coi như nhiễu — tránh biến dao động quanh trung bình thành "xu hướng".
MIN_MEANINGFUL_SLOPE = 0.05
DEFAULT_SAMPLE_INTERVAL_SEC = 3600


@dataclass(frozen=True)
class CapacityAdvice:
    tenant_id: str
    host: str
    metric: str
    action: str
    summary: str
    current: float | None = None
    mean: float | None = None
    slope_per_sample: float = 0.0
    days_to_threshold: float | None = None
    urgent: bool = False
    auto_execute: bool = False  # bất biến — xem docstring module
    evidence: dict[str, Any] = field(default_factory=dict)


def _numeric(samples: list[Any]) -> list[float]:
    """Bỏ mẫu hỏng thay vì ném lỗi — chuỗi baseline đến từ agent ngoài, không tin được."""
    out: list[float] = []
    for s in samples or []:
        try:
            if s is None or isinstance(s, bool):
                continue
            out.append(float(s))
        except (TypeError, ValueError):
            continue
    return out


def _linear_slope(values: list[float]) -> float:
    """Độ dốc hồi quy tuyến tính đơn giản (least squares) theo chỉ số mẫu."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return (num / den) if den else 0.0


def _is_monotonic_growth(values: list[float]) -> bool:
    """Không có lần nào giảm đáng kể → dấu hiệu tích luỹ, không phải tải dao động."""
    # zip cặp liền kề: values[1:] ngắn hơn 1 phần tử là CỐ Ý, nên không dùng strict=True.
    drops = sum(1 for a, b in zip(values, values[1:]) if b < a - 1e-9)  # noqa: B905
    return drops == 0


def analyze_capacity(
    *,
    samples: list[Any],
    metric: str,
    host: str,
    tenant_id: str,
    threshold: float = DEFAULT_THRESHOLD,
    scale_down_ceiling: float = DEFAULT_SCALE_DOWN_CEILING,
    sample_interval_sec: int = DEFAULT_SAMPLE_INTERVAL_SEC,
) -> CapacityAdvice:
    """Phân tích chuỗi mẫu → đề xuất scale. Không bao giờ tự thực thi."""
    values = _numeric(samples)

    def _advice(action: str, summary: str, **kw: Any) -> CapacityAdvice:
        return CapacityAdvice(
            tenant_id=tenant_id, host=host, metric=metric,
            action=action, summary=summary, **kw,
        )

    if len(values) < MIN_SAMPLES:
        return _advice(
            ACTION_INSUFFICIENT_DATA,
            f"Chỉ có {len(values)} mẫu hợp lệ (cần ≥ {MIN_SAMPLES}) — chưa đủ để kết luận.",
            evidence={"samples": len(values), "min_samples": MIN_SAMPLES},
        )

    current = values[-1]
    mean = sum(values) / len(values)
    slope = _linear_slope(values)
    evidence: dict[str, Any] = {
        "samples": len(values),
        "current": round(current, 4),
        "mean": round(mean, 4),
        "slope_per_sample": round(slope, 6),
        "threshold": threshold,
        "sample_interval_sec": sample_interval_sec,
    }

    # Đã vượt ngưỡng: khẩn cấp, không cần chờ dự báo.
    if current >= threshold:
        return _advice(
            ACTION_SCALE_UP,
            f"`{metric}` đang ở {current:.1f} — đã vượt ngưỡng {threshold:.0f}. Cần xử lý ngay.",
            current=current, mean=mean, slope_per_sample=slope,
            days_to_threshold=0.0, urgent=True, evidence=evidence,
        )

    rising = slope > MIN_MEANINGFUL_SLOPE
    falling = slope < -MIN_MEANINGFUL_SLOPE

    if rising:
        samples_left = (threshold - current) / slope
        days = samples_left * sample_interval_sec / 86400.0
        evidence["days_to_threshold"] = round(days, 2)

        # Bộ nhớ tăng đơn điệu là dấu hiệu rò rỉ. Thêm RAM chỉ dời ngày sập —
        # đây là lúc một senior SRE nói "đừng scale, đi tìm chỗ rò".
        if metric.lower().startswith("mem") and _is_monotonic_growth(values):
            return _advice(
                ACTION_INVESTIGATE_LEAK,
                f"`{metric}` tăng đơn điệu từ {values[0]:.1f} → {current:.1f}, không lần nào "
                f"giảm — nghi rò rỉ bộ nhớ. Dự kiến chạm {threshold:.0f} sau ~{days:.1f} ngày. "
                "Nên tìm nguyên nhân rò rỉ trước khi cấp thêm tài nguyên.",
                current=current, mean=mean, slope_per_sample=slope,
                days_to_threshold=days, urgent=days < 3, evidence=evidence,
            )

        return _advice(
            ACTION_SCALE_UP,
            f"`{metric}` đang tăng (hiện {current:.1f}, trung bình {mean:.1f}); dự kiến chạm "
            f"ngưỡng {threshold:.0f} sau ~{days:.1f} ngày. Nên chuẩn bị nâng dung lượng.",
            current=current, mean=mean, slope_per_sample=slope,
            days_to_threshold=days, urgent=days < 3, evidence=evidence,
        )

    if not falling and mean < scale_down_ceiling:
        return _advice(
            ACTION_SCALE_DOWN,
            f"`{metric}` ổn định ở mức thấp (trung bình {mean:.1f} < {scale_down_ceiling:.0f}) "
            "trong toàn bộ cửa sổ quan sát — có thể thu hẹp tài nguyên để tiết kiệm.",
            current=current, mean=mean, slope_per_sample=slope, evidence=evidence,
        )

    trend = "giảm" if falling else "ổn định"
    return _advice(
        ACTION_HOLD,
        f"`{metric}` {trend} (hiện {current:.1f}, trung bình {mean:.1f}) — chưa cần đổi dung lượng.",
        current=current, mean=mean, slope_per_sample=slope, evidence=evidence,
    )
