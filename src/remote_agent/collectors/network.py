"""Remote agent collector — mạng (domain=network).

Probe:
  network_listeners → domain=network. FAILED khi một cổng ĐANG LẮNG NGHE biến mất
                      giữa hai chu kỳ thu.

Vì sao collector này tồn tại (đo được trên VM thật 2026-07-30): domain `network` là
domain DUY NHẤT có lệnh trong catalogue nhưng **không có detector nào phát cảnh báo**.
`port_scan`/`connection_scan` là `signal_type=DISCOVERY` — chúng đi vào knowledge
pipeline, không phải đường sự cố. Nên khi nginx bị dừng và cổng 80 đóng, Omni chỉ thấy
điều đó ở lần re-discovery kế tiếp (mỗi 1h) như một *thay đổi topology*, không phải sự
cố cần chẩn đoán.

Nguyên tắc phát hiện — CHUYỂN TRẠNG THÁI, không phải danh sách cổng mong đợi:
một cổng đang mở rồi biến mất là sự thật quan sát được, không cần ai khai báo trước
"cổng nào phải mở". Danh sách mong đợi sẽ lệch ngay khi khách thêm/bớt dịch vụ, và một
cấu hình lệch thì hoặc bỏ sót hoặc báo nhầm — cả hai đều tệ hơn không có.

Đánh đổi có chủ đích: chu kỳ ĐẦU sau khi agent khởi động không báo gì (chưa có trí nhớ).

Read-only. Dùng asyncio.create_subprocess_exec — không blocking subprocess.run().
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pkg.domain.taxonomy import NETWORK
from remote_agent import exec_guard
from remote_agent.evidence import build_envelope

logger = logging.getLogger(__name__)

# Cổng ephemeral của client — không phải dịch vụ, không theo dõi.
_EPHEMERAL_MIN = 32768

# Trí nhớ một chu kỳ: {(proto, port)} đang lắng nghe ở lần thu trước.
_prev_listeners: set[tuple[str, int]] | None = None


def _reset_listener_memory() -> None:
    """Chỉ dùng cho test — xoá trí nhớ chu kỳ trước."""
    global _prev_listeners
    _prev_listeners = None


async def _run(cmd: list[str], timeout: float = 8.0) -> tuple[str, str, int]:
    """Chạy lệnh read-only. Không bao giờ ném."""
    # Cùng validator với command channel — collector KHÔNG có đường riêng.
    reason = exec_guard.check(cmd)
    if reason:
        return "", f"blocked: {reason}", 1
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        return "", "timeout", 1
    except Exception as exc:  # noqa: BLE001 — collector không được làm sập agent
        return "", str(exc), 1


def parse_listeners(ss_output: str) -> set[tuple[str, int]]:
    """Tách ``(proto, port)`` từ đầu ra ``ss -lntu``.

    Lấy cổng ở cột địa chỉ cục bộ bằng cách cắt sau dấu ``:`` CUỐI — địa chỉ IPv6 có
    nhiều dấu hai chấm (``[::]:80``), nên cắt ở dấu đầu tiên sẽ ra rác.

    Gộp IPv4 và IPv6 của cùng một cổng thành một mục: một dịch vụ bind cả hai họ địa chỉ
    không phải hai dịch vụ, và báo hai lần khi nó tắt là báo nhầm một lần.
    """
    found: set[tuple[str, int]] = set()
    for line in ss_output.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto = parts[0].lower()
        if proto not in ("tcp", "udp"):
            continue  # bỏ dòng tiêu đề (Netid/State/...)
        local = parts[4]
        if ":" not in local:
            continue
        try:
            port = int(local.rsplit(":", 1)[1])
        except ValueError:
            continue
        if port >= _EPHEMERAL_MIN:
            continue
        found.add((proto, port))
    return found


async def collect_listening_ports(hostname: str) -> dict[str, Any] | None:
    """Phát hiện cổng lắng nghe vừa biến mất. ``None`` khi không đọc được ``ss``."""
    global _prev_listeners

    out, err, rc = await _run(["ss", "-lntu"])
    if rc != 0:
        logger.warning("[collector.network] ss unavailable: %s", err[:200])
        return None

    now = parse_listeners(out)
    prev = _prev_listeners
    _prev_listeners = now

    lost = sorted(prev - now) if prev is not None else []
    gained = sorted(now - prev) if prev is not None else []

    fact: dict[str, Any] = {
        "listening_count": len(now),
        "listening_ports": sorted(p for _proto, p in now),
        "lost_listeners": [f"{proto}/{port}" for proto, port in lost],
        "lost_count": len(lost),
        "new_listeners": [f"{proto}/{port}" for proto, port in gained],
        "first_cycle": prev is None,
    }
    # `result` phải là đúng chuỗi "FAILED" để `assess_domain_severity` nâng urgency —
    # xem ghi chú cùng nội dung ở `workers/knowledge_pipeline.py`.
    fact["result"] = "FAILED" if lost else "PASSED"

    if lost:
        hint = (
            f"[{hostname}] mạng: {len(lost)} cổng lắng nghe VỪA ĐÓNG: "
            f"{fact['lost_listeners'][:5]} (còn {len(now)} cổng đang mở)"
        )
    else:
        hint = f"[{hostname}] mạng: {len(now)} cổng đang lắng nghe, không cổng nào đóng"

    return build_envelope(
        probe="network_listeners",
        lane="SYS_HARD_FAIL" if lost else "SYS_RESOURCE",
        domain=NETWORK,
        result="FAILED" if lost else "PASSED",
        extracted_fact=fact,
        alert_rule="NetworkListenerLost" if lost else "NetworkListenersHealthy",
        alert_hint=hint,
        symptom_group="network_state",
        namespace=hostname,
        raw=out[:4000],
    )
