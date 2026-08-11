"""Remote agent collector — services health (HAProxy, systemd units).

Probes:
  service_haproxy        → domain=service  (lane deprecated: SYS_HARD_FAIL / SYS_RESOURCE)
  service_systemd_units  → domain=service. lane=SYS_HARD_FAIL if any unit
                            failed/activating, else SYS_RESOURCE. Which failed
                            units are the customer's own app (vs a base OS
                            package) is determined per-unit via the real
                            package manager (pkg_origin.py) — never a
                            hardcoded service-name list.

All commands are read-only; no mutations.
Uses asyncio.create_subprocess_exec — no blocking subprocess.run().
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from remote_agent import pkg_origin
from remote_agent import exec_guard
from pkg.domain.taxonomy import SERVICE
from remote_agent.evidence import build_envelope

logger = logging.getLogger(__name__)

_HAPROXY_STATS_SOCKET = "/run/haproxy/admin.sock"
_HAPROXY_STATS_PORT = 9000  # prometheus-haproxy-exporter default


async def _run(cmd: list[str], stdin: str | None = None, timeout: float = 8.0) -> tuple[str, str, int]:
    """Run subprocess, optionally pipe stdin. Never raises."""
    # Cùng validator với command channel — collector KHÔNG có đường riêng.
    reason = exec_guard.check(cmd)
    if reason:
        return "", f"blocked: {reason}", 1
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        in_bytes = stdin.encode() if stdin else None
        out, err = await asyncio.wait_for(proc.communicate(in_bytes), timeout=timeout)
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        return "", "timeout", 1
    except Exception as exc:
        return "", str(exc), 1


async def _query_haproxy_socket(socket_path: str, command: str, timeout: float = 5.0) -> tuple[str, str, int]:
    """Query HAProxy stats socket via Python asyncio (no socat dependency)."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path), timeout=timeout
        )
        try:
            writer.write(command.encode())
            await writer.drain()
            chunks: list[bytes] = []
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode(errors="replace"), "", 0
        finally:
            writer.close()
            await writer.wait_closed()
    except asyncio.TimeoutError:
        return "", "timeout", 1
    except Exception as exc:
        return "", str(exc), 1


async def collect_haproxy_stats(hostname: str) -> dict[str, Any] | None:
    """Collect HAProxy CSV stats via unix socket (read-only 'show stat')."""
    # Try Python asyncio unix socket first (no socat dependency)
    out, err, rc = await _query_haproxy_socket(_HAPROXY_STATS_SOCKET, "show stat\n")

    if rc != 0:
        logger.debug("[collector.services] haproxy socket unavailable, trying http stats: %s", err[:100])
        out, err, rc = await _run(
            ["curl", "-sf", f"http://127.0.0.1:{_HAPROXY_STATS_PORT}/metrics"],
        )
        if rc != 0:
            logger.warning("[collector.services] haproxy stats unavailable: %s", err[:200])
            return None
        return _parse_haproxy_prom_metrics(out, hostname)

    return _parse_haproxy_csv(out, hostname)


def _parse_haproxy_csv(csv_text: str, hostname: str) -> dict[str, Any]:
    """Parse HAProxy CSV stat output into fact dict."""
    lines = [l for l in csv_text.splitlines() if l and not l.startswith("#")]
    down_backends: list[str] = []
    total_sessions = 0
    total_bytes_in = 0

    for line in lines:
        cols = line.split(",")
        if len(cols) < 20:
            continue
        pxname, svname, status = cols[0], cols[1], cols[17] if len(cols) > 17 else ""
        scur = cols[4] if len(cols) > 4 else "0"
        bin_val = cols[8] if len(cols) > 8 else "0"
        try:
            total_sessions += int(scur or 0)
            total_bytes_in += int(bin_val or 0)
        except ValueError:
            pass
        if svname not in ("FRONTEND", "BACKEND") and status and "DOWN" in status.upper():
            down_backends.append(f"{pxname}/{svname}")

    fact: dict[str, Any] = {
        "service": "haproxy",
        "down_backends": down_backends,
        "down_backend_count": len(down_backends),
        "total_current_sessions": total_sessions,
        "total_bytes_in": total_bytes_in,
    }

    anomalies = []
    if down_backends:
        anomalies.append(f"backends_down={down_backends[:5]}")

    result = "FAILED" if anomalies else "PASSED"
    hint = f"[{hostname}] HAProxy — " + (", ".join(anomalies) if anomalies else f"sessions={total_sessions} all backends UP")

    return build_envelope(
        probe="service_haproxy",
        lane="SYS_HARD_FAIL" if down_backends else "SYS_RESOURCE",
        domain=SERVICE,
        result=result,
        extracted_fact=fact,
        alert_rule="HAProxyBackendDown" if down_backends else "HAProxyHealthy",
        alert_hint=hint,
        symptom_group="service_state",
        namespace=hostname,
    )


def _parse_haproxy_prom_metrics(prom_text: str, hostname: str) -> dict[str, Any]:
    """Minimal Prometheus text format parser for HAProxy exporter."""
    down_backends: list[str] = []
    for line in prom_text.splitlines():
        if line.startswith("haproxy_server_up") and ' 0' in line:
            down_backends.append(line.split("{", 1)[-1].split("}")[0] if "{" in line else "unknown")

    fact: dict[str, Any] = {"service": "haproxy", "down_backends": down_backends, "down_backend_count": len(down_backends)}
    result = "FAILED" if down_backends else "PASSED"
    hint = f"[{hostname}] HAProxy (prom) — " + (f"backends_down={down_backends[:5]}" if down_backends else "all UP")

    return build_envelope(
        probe="service_haproxy",
        lane="SYS_HARD_FAIL" if down_backends else "SYS_RESOURCE",
        domain=SERVICE,
        result=result,
        extracted_fact=fact,
        alert_rule="HAProxyBackendDown" if down_backends else "HAProxyHealthy",
        alert_hint=hint,
        symptom_group="service_state",
        namespace=hostname,
    )


# Trí nhớ một chu kỳ: unit đang `active` ở lần thu trước. Cần thiết vì "đáng ra phải
# chạy" KHÔNG suy được từ metadata systemd — xem docstring dưới.
_prev_active_units: set[str] | None = None

# Unit ĐÃ XÁC NHẬN dừng và CHƯA chạy lại — outage đang diễn ra.
#
# Vì sao cần, đo được trên UAT 2026-08-11: `payment-api` ở trạng thái `enabled`+`inactive`
# (đang chết thật) nhưng collector trả "all monitored services OK"/`PASSED`. Phép trừ tập
# `gone = prev - now_active` là EDGE-TRIGGERED: nó bắn đúng một lần lúc chuyển trạng thái,
# các chu kỳ sau unit không còn trong `prev` lẫn `now_active` nên `gone` rỗng vĩnh viễn.
# Hậu quả thật: (1) sự cố kéo dài chỉ được báo 1 lần, mà 77% lượt chẩn đoán chết vì LLM
# timeout ⇒ sự cố biến mất khỏi radar; (2) vòng tự khắc phục không bao giờ chạy lại được
# vì không còn evidence.
#
# Chỉ chứa unit ĐÃ QUA bộ lọc oneshot/`RemainAfterExit` ở dưới, nên nó KHÔNG làm sống lại
# 15 unit nhiễu (`systemd-pcrlock-*`, `dmesg` Type=idle) mà cách "quét enabled+inactive"
# từng gây ra — xem docstring `_collect_units_that_stopped`.
_known_stopped_units: set[str] = set()


def _reset_service_state_memory() -> None:
    """Chỉ dùng cho test — xoá trí nhớ chu kỳ trước VÀ danh sách outage đang mở."""
    global _prev_active_units
    _prev_active_units = None
    _known_stopped_units.clear()


# Kiểu `Type=` của một daemon THƯỜNG TRÚ. Cố ý dùng ALLOWLIST chứ không denylist:
# kiểu lạ mặc định bị coi là "không phải daemon" ⇒ nghiêng về ÍT nhiễu. Bỏ sót một
# daemon kiểu hiếm vẫn được edge-trigger bắt lại ngay khi nó dừng trong lúc agent theo
# dõi — nên đánh đổi này không tạo điểm mù mới.
_DAEMON_TYPES = frozenset({"simple", "notify", "forking", "exec", "dbus", "notify-reload"})


async def _collect_already_down_units() -> list[str]:
    """Unit `enabled` + `inactive` NGAY TỪ ĐẦU — outage có trước khi agent khởi động.

    Edge-trigger về bản chất không thể thấy loại này: không có "chu kỳ trước" để so.
    Ca thật 2026-08-11: `payment-api` chết lúc 12:53, agent restart 13:14 ⇒ collector
    trả "all monitored services OK" trong khi dịch vụ vẫn đang chết.

    Ghi chú gốc cho rằng cách này bất khả thi vì nhiễu ("15 unit... KHÔNG có thuộc tính
    systemd nào phân biệt daemon thường trú với chạy một lần"). Đo lại trên `cust-app`
    2026-08-11: 18 unit `enabled`+`inactive`, áp CẢ HAI bộ lọc thì còn **đúng 1**
    (`payment-api`) — 0 false positive:
      - `ConditionResult=no` loại 13 (`systemd-pcrlock-*`×7, `timesyncd`, `sysext`, …)
      - `Type` ngoài `_DAEMON_TYPES` loại `dmesg`(idle) + `e2scrub_reap`(oneshot)
      - unit template `@.service` loại `getty@`
    Tức `Type` + `ConditionResult` CỘNG LẠI thì phân biệt được — ghi chú gốc chỉ đúng khi
    dùng riêng `ConditionResult`.
    """
    out, err, rc = await _run([
        "systemctl", "list-unit-files", "--type=service",
        "--state=enabled", "--no-legend", "--no-pager", "--plain",
    ], timeout=20.0)
    if rc != 0:
        logger.warning("[collector.services] list-unit-files enabled unavailable: %s", err[:200])
        return []

    down: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        unit_full = parts[0]
        if "@." in unit_full:      # template, không phải unit chạy được
            continue
        act, _, _ = await _run(["systemctl", "is-active", unit_full], timeout=5.0)
        if act.strip() == "active":
            continue
        out_p, _, rc_p = await _run(
            ["systemctl", "show", "-p", "ConditionResult", "-p", "Type",
             "-p", "RemainAfterExit", unit_full],
            timeout=5.0,
        )
        props = dict(
            l.split("=", 1) for l in out_p.splitlines() if "=" in l
        ) if rc_p == 0 else {}
        # systemd tự nói unit này không áp dụng cho máy này ⇒ inactive là ĐÚNG.
        if props.get("ConditionResult") == "no":
            continue
        if props.get("Type", "") not in _DAEMON_TYPES:
            continue
        down.append(unit_full.removesuffix(".service"))
    return down


async def _collect_units_that_stopped() -> tuple[list[str], list[str]]:
    """Unit vừa CHUYỂN từ đang chạy sang dừng — outage mà `--state=failed` không thấy.

    Vì sao cần: ``systemctl stop nginx`` để lại trạng thái **``inactive``**, không phải
    ``failed``. Nên một dịch vụ bị dừng — do người, do OOM-killer, do deploy lỗi — là sự
    cố thật nhưng KHÔNG sinh bằng chứng nào. Đã kiểm trực tiếp trên 3 VM (2026-07-30):
    nginx/mariadb/payment-api đều `inactive`, còn `list-units --state=failed,activating`
    trả về **rỗng**.

    Vì sao dùng CHUYỂN TRẠNG THÁI thay vì "enabled nhưng inactive": đã thử cách đó trên
    VM thật và nó báo **15 unit**, gần hết là nhiễu —
    ``systemd-pcrlock-*``/``systemd-timesyncd`` (`ConditionResult=no`: systemd nói không
    áp dụng cho máy này) và ``dmesg`` (`Type=idle`, chạy lúc boot rồi thoát theo thiết kế).
    Lọc ``ConditionResult`` loại được nhóm đầu, nhưng KHÔNG có thuộc tính systemd nào
    phân biệt "daemon thường trú" với "chạy một lần" một cách tổng quát — ``dmesg`` trông
    y như một daemon đã bị dừng.

    "Đang chạy ở chu kỳ trước, giờ không chạy" thì không cần suy đoán gì: nó là sự thật
    quan sát được. Đánh đổi có chủ đích: chu kỳ ĐẦU sau khi agent khởi động không báo gì
    (chưa có trí nhớ) — thà bỏ sót một chu kỳ hơn là bơm 15 báo nhầm mỗi chu kỳ, vì báo
    nhầm dày sẽ dạy người vận hành bỏ qua cảnh báo.

    Trả ``(stopped, skipped_oneshot)``.
    """
    global _prev_active_units

    out_act, err, rc = await _run([
        "systemctl", "list-units", "--type=service",
        "--state=active", "--no-legend", "--no-pager", "--plain",
    ], timeout=20.0)
    if rc != 0:
        logger.warning("[collector.services] list-units active unavailable: %s", err[:200])
        return [], []

    # Loại unit TEMPLATE (`getty@.service`): không phải một unit chạy được.
    now_active = {
        p[0] for p in (line.split() for line in out_act.splitlines())
        if p and "@." not in p[0]
    }

    prev = _prev_active_units
    _prev_active_units = now_active

    # Unit đã biết đang dừng mà nay chạy lại ⇒ hết sự cố, thôi báo. Không xoá là cảnh
    # báo kẹt vĩnh viễn — tệ hơn không có cảnh báo.
    _known_stopped_units.difference_update(now_active)
    _known_stopped_units.difference_update(u.removesuffix(".service") for u in now_active)

    if prev is None:
        # Chu kỳ đầu sau khi agent khởi động: chưa có gì để SO. Quét level-triggered để
        # không mù trước outage ĐÃ TỒN TẠI từ trước — chỉ chạy đúng 1 lần mỗi vòng đời
        # tiến trình nên chi phí `systemctl show` mỗi unit là chấp nhận được.
        _known_stopped_units.update(await _collect_already_down_units())
        return sorted(_known_stopped_units), []

    # Phép TRỪ trên tập — không giả định thứ tự đầu ra của bất kỳ lệnh nào. Ghép theo vị
    # trí (`zip` với đầu ra `systemctl is-active <nhiều unit>`) từng làm sai âm thầm:
    # một unit bị bỏ dòng là cả danh sách lệch một bậc, và ta gán trạng thái unit này cho
    # unit khác. Đã trả giá 2026-07-30 — `nginx` bị dừng nhưng công cụ báo `dmesg`.
    gone = prev - now_active
    if not gone:
        # KHÔNG return [] ở đây: outage phát hiện từ chu kỳ trước vẫn đang mở và phải
        # được báo lại. Đây chính là dòng từng làm sự cố kéo dài tàng hình.
        return sorted(_known_stopped_units), []

    stopped: list[str] = []
    skipped_oneshot: list[str] = []
    for unit_full in sorted(gone):
        # Đọc theo KHOÁ, không theo vị trí: `--value` trả thuộc tính theo thứ tự của
        # systemd chứ không theo thứ tự `-p` được truyền, nên đọc vals[0]/vals[1] là
        # cùng một lớp lỗi ghép-theo-vị-trí.
        out_p, _, rc_p = await _run(
            ["systemctl", "show", "-p", "Type", "-p", "RemainAfterExit", unit_full],
            timeout=5.0,
        )
        props = dict(
            line.split("=", 1) for line in out_p.splitlines() if "=" in line
        ) if rc_p == 0 else {}
        # `oneshot` thoát sau khi xong là ĐÚNG — nó biến mất khỏi danh sách active theo
        # thiết kế, không phải outage.
        if props.get("Type") == "oneshot" and props.get("RemainAfterExit") != "yes":
            skipped_oneshot.append(unit_full.removesuffix(".service"))
            continue
        stopped.append(unit_full.removesuffix(".service"))

    # Ghi nhớ để các chu kỳ sau vẫn báo, tới khi unit chạy lại.
    _known_stopped_units.update(stopped)
    return sorted(_known_stopped_units), skipped_oneshot


async def collect_systemd_units(hostname: str) -> dict[str, Any] | None:
    """Collect failed / degraded systemd units (read-only).

    Any failed/activating unit is a hard failure worth surfacing — severity
    doesn't depend on the unit's name matching a hardcoded list, which can
    never know a customer's own service names in advance. Instead, each
    failed unit's origin (base OS package vs the customer's own app) is
    determined per-unit via the real package manager (pkg_origin.py) and
    reported as evidence context, not used to gate whether it's reported.
    """
    out, err, rc = await _run([
        "systemctl", "list-units",
        "--type=service",
        "--state=failed,activating",
        "--no-legend", "--no-pager",
        "--plain",
    ])
    if rc != 0:
        logger.warning("[collector.services] systemctl unavailable: %s", err[:200])
        return None

    failed: list[str] = []
    ignored_disabled: list[str] = []
    origin_by_unit: dict[str, str] = {}
    custom_failed: list[str] = []

    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        unit_full = parts[0]
        unit = unit_full.removesuffix(".service")
        # Migration residue guard: a unit that is BOTH disabled AND failed was
        # stopped intentionally (e.g. agent migration) — systemd keeps the
        # failed state until reset-failed. Not an incident; report separately.
        out_en, _, _ = await _run(["systemctl", "is-enabled", unit_full], timeout=5.0)
        if out_en.strip() in ("disabled", "masked"):
            ignored_disabled.append(unit)
            continue
        failed.append(unit)
        fragment_path = await pkg_origin.get_fragment_path(unit_full)
        origin = await pkg_origin.classify_unit_origin(fragment_path)
        origin_by_unit[unit] = origin
        if not origin.startswith("package:"):
            custom_failed.append(unit)

    stopped, skipped_oneshot = await _collect_units_that_stopped()

    # `enabled` + `inactive` cũng là FAILED: người vận hành đã tuyên bố unit phải chạy.
    result = "FAILED" if (failed or stopped) else "PASSED"
    fact: dict[str, Any] = {
        "result": result,
        "failed_units": failed,
        "failed_count": len(failed),
        # Dịch vụ VỪA chuyển từ đang chạy sang dừng — xem `_collect_units_that_stopped`.
        "stopped_units": stopped,
        "stopped_count": len(stopped),
        "skipped_oneshot_units": skipped_oneshot,
        # Kept for backward compat with downstream consumers that check
        # truthiness (os_state_validator.py) — now means "failed units not
        # owned by a distro package", i.e. very likely the customer's own
        # app, not "matched a hardcoded infra name".
        "critical_failed_units": custom_failed,
        "failed_units_origin": origin_by_unit,
        "ignored_disabled_units": ignored_disabled,
    }
    if failed or stopped:
        parts = []
        if failed:
            parts.append(f"{len(failed)} units failed/activating")
            if custom_failed:
                parts.append(f"CUSTOM_APP: {custom_failed}")
        if stopped:
            parts.append(f"{len(stopped)} dich vu VUA DUNG (dang chay -> khong chay): {stopped[:5]}")
        hint = f"[{hostname}] systemd: " + " · ".join(parts)
    else:
        hint = f"[{hostname}] systemd: all monitored services OK"

    return build_envelope(
        probe="service_systemd_units",
        lane="SYS_HARD_FAIL" if (failed or stopped) else "SYS_RESOURCE",
        domain=SERVICE,
        result=result,
        extracted_fact=fact,
        alert_rule=(
            "SystemdUnitsFailed" if failed
            else "SystemdUnitsStopped" if stopped
            else "SystemdHealthy"
        ),
        alert_hint=hint,
        symptom_group="service_state",
        namespace=hostname,
    )
