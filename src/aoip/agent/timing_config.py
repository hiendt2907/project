"""Timing model cho long-running execution safety — Living Operations Runtime.

Vì sao tồn tại: có 4 timer ĐỘC LẬP quanh một mutation dài (xem bảng dưới); nếu chúng
không được cấu hình có chủ ý cùng nhau, một operation chạy lâu hơn TTL của lease/
visibility trong khi vẫn chưa xong action timeout → Gateway redeliver / lease bị agent
khác giành trong khi mutation cũ vẫn chạy. Renewal đóng khoảng trống đó; module này chỉ
giữ CÁC CON SỐ có validate, không phải framework config mới.

| Timer                | Owner         | Ý nghĩa hết hạn                                             |
|-----------------------|---------------|--------------------------------------------------------------|
| execution_lease_ttl_s | Agent (Redis) | Owner khác có thể acquire lease → concurrent mutation risk    |
| renewal_interval_s    | Agent (local) | Không renew kịp → lease/visibility tự hết trước lần renew sau |
| visibility_extension_s| Gateway       | Redelivery: attempt mới + token mới được claim                |
| action_timeout_s      | Agent (transport.run) | Subprocess mutation bị kill/timeout                    |

Không tăng TTL thành giá trị rất lớn để "giải quyết" vấn đề — renewal (chạy trong khi
mutation còn sống) là cơ chế đúng; TTL lớn chỉ trì hoãn phát hiện agent chết.
"""
from __future__ import annotations

from dataclasses import dataclass

# Safety margin tối thiểu: renewal phải chạy nhiều lần trước khi TTL/visibility hết,
# không phải "vừa đủ 1 lần" (jitter/GC pause có thể làm renew trễ).
_MIN_RENEWAL_MARGIN = 2.0


class InvalidTimingConfig(ValueError):
    """Combination TTL/interval không an toàn — fail-closed, KHÔNG khởi động với config này."""


@dataclass(frozen=True)
class TimingConfig:
    execution_lease_ttl_s: float = 120.0
    lease_renewal_interval_s: float = 30.0
    gateway_visibility_s: float = 60.0
    visibility_renewal_interval_s: float = 15.0
    action_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        for name in ("execution_lease_ttl_s", "lease_renewal_interval_s",
                    "gateway_visibility_s", "visibility_renewal_interval_s",
                    "action_timeout_s"):
            value = getattr(self, name)
            if value <= 0:
                raise InvalidTimingConfig(f"{name} phải > 0, nhận {value!r}")
        if self.lease_renewal_interval_s >= self.execution_lease_ttl_s:
            raise InvalidTimingConfig(
                "lease_renewal_interval_s phải nhỏ hơn execution_lease_ttl_s "
                f"({self.lease_renewal_interval_s} >= {self.execution_lease_ttl_s})")
        if self.visibility_renewal_interval_s >= self.gateway_visibility_s:
            raise InvalidTimingConfig(
                "visibility_renewal_interval_s phải nhỏ hơn gateway_visibility_s "
                f"({self.visibility_renewal_interval_s} >= {self.gateway_visibility_s})")
        if self.execution_lease_ttl_s < _MIN_RENEWAL_MARGIN * self.lease_renewal_interval_s:
            raise InvalidTimingConfig(
                "execution_lease_ttl_s cần ít nhất "
                f"{_MIN_RENEWAL_MARGIN}x lease_renewal_interval_s (safety margin)")
        if self.gateway_visibility_s < _MIN_RENEWAL_MARGIN * self.visibility_renewal_interval_s:
            raise InvalidTimingConfig(
                "gateway_visibility_s cần ít nhất "
                f"{_MIN_RENEWAL_MARGIN}x visibility_renewal_interval_s (safety margin)")
