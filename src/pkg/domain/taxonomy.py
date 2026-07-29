"""Taxonomy domain DUY NHẤT của Omni.

Thiết kế + hiện trạng đã khảo sát: `plans/unify-domain-and-diagnostic-catalog-2026-07-30.md`.

Trước module này, "domain" có BA từ vựng không cầu nối:
  - `aoip/domain_adapters.py`      → linux, kubernetes, database, network
  - `pkg/reasoning/domain_signals` → os_system, network, storage, services,
                                     container_logs, database, application, security
  - `schemas/playbook.py`          → k8s, os, network, service, application, api, hardware

Ba tên cho K8s (`kubernetes`/`k8s`/`container_logs`), ba tên cho OS
(`linux`/`os`/`os_system`), `service` vs `services`. Không có hàm map nào, nên không
thể trả lời "Omni làm được gì trên domain X" mà không đọc cả ba file.

Đặt ở `src/pkg/` vì CẢ `gateway/` và `workers/` đều cần, mà gateway không được import
workers (INVARIANT). Không đặt ở `aoip/` vì `remote_agent` cũng dùng.

NGUYÊN TẮC: chuẩn hoá khi ĐỌC, luôn ghi bằng canonical. Không xoá tên cũ ở biên —
payload agent phiên bản cũ và dữ liệu lịch sử vẫn phải hiểu được.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Canonical — nguồn sự thật duy nhất
# ---------------------------------------------------------------------------

KUBERNETES: Final = "kubernetes"
OS_HOST: Final = "os_host"
NETWORK: Final = "network"
STORAGE: Final = "storage"
DATABASE: Final = "database"
SERVICE: Final = "service"
APPLICATION: Final = "application"
SECURITY: Final = "security"
HARDWARE: Final = "hardware"
UNKNOWN: Final = "unknown"

CANONICAL_DOMAINS: Final[tuple[str, ...]] = (
    KUBERNETES, OS_HOST, NETWORK, STORAGE, DATABASE,
    SERVICE, APPLICATION, SECURITY, HARDWARE,
)

# `unknown` KHÔNG nằm trong CANONICAL_DOMAINS: nó là trạng thái "chưa phân loại
# được", không phải một domain để trao quyền. Ai lặp qua danh sách domain để cấp
# quyền hoặc dựng báo cáo năng lực đều không nên thấy nó.
ALL_DOMAINS: Final[tuple[str, ...]] = CANONICAL_DOMAINS + (UNKNOWN,)


# ---------------------------------------------------------------------------
# Alias — mọi giá trị của ba từ vựng cũ
# ---------------------------------------------------------------------------

_ALIASES: Final[dict[str, str]] = {
    # từ aoip/domain_adapters.py
    "linux": OS_HOST,
    "kubernetes": KUBERNETES,
    # từ pkg/reasoning/domain_signals.py
    "os_system": OS_HOST,
    "services": SERVICE,
    "container_logs": KUBERNETES,
    # từ schemas/playbook.py
    "k8s": KUBERNETES,
    "os": OS_HOST,
    "api": APPLICATION,
    # biến thể hay gặp trong payload agent / prompt LLM
    "k8s_cluster": KUBERNETES,
    "container": KUBERNETES,
    "docker": KUBERNETES,
    "host": OS_HOST,
    "vm": OS_HOST,
    "baremetal": OS_HOST,
    "os_baremetal": OS_HOST,
    "net": NETWORK,
    "disk": STORAGE,
    "filesystem": STORAGE,
    "fs": STORAGE,
    "db": DATABASE,
    "sql": DATABASE,
    "systemd": SERVICE,
    "app": APPLICATION,
    "http": APPLICATION,
    "siem": SECURITY,
    "sec": SECURITY,
    "hw": HARDWARE,
}


def normalize_domain(value: str | None) -> str:
    """Đưa bất kỳ tên domain nào về canonical. Không nhận ra ⇒ ``unknown``.

    Trả ``unknown`` thay vì ném lỗi có chủ đích: hàm này nằm trên đường đọc dữ liệu
    lịch sử và payload agent phiên bản cũ. Làm vỡ đường đó để phạt một cái tên lạ là
    đổi một nhãn sai thành một sự cố mất dữ liệu.

    Chỗ nào KHÔNG được im lặng thì dùng ``require_domain``.
    """
    v = (value or "").strip().lower().replace("-", "_")
    if not v:
        return UNKNOWN
    if v in CANONICAL_DOMAINS:
        return v
    if v == UNKNOWN:
        return UNKNOWN
    return _ALIASES.get(v, UNKNOWN)


def require_domain(value: str | None) -> str:
    """Như ``normalize_domain`` nhưng ném ``ValueError`` khi không nhận ra.

    Dùng ở đường GHI (catalogue, migration, API nhận domain từ client) — ghi một
    domain rác vào nguồn sự thật thì mọi báo cáo dựng trên nó đều lệch, mà không có
    lỗi nào bật ra để ai đó phát hiện.
    """
    d = normalize_domain(value)
    if d == UNKNOWN and (value or "").strip().lower() != UNKNOWN:
        raise ValueError(
            f"domain khong nhan ra: {value!r} — them alias vao pkg.domain.taxonomy "
            f"hoac dung mot trong: {', '.join(CANONICAL_DOMAINS)}"
        )
    return d


def is_canonical(value: str | None) -> bool:
    return (value or "") in CANONICAL_DOMAINS


# ---------------------------------------------------------------------------
# LearningTrack — khái niệm TỪNG BỊ LẪN vào cột `domain`
# ---------------------------------------------------------------------------
# `playbook_graduation.domain` đang chứa cả `k8s` (domain kỹ thuật, do
# playbook_governor ghi qua Redis) lẫn `advisory` (NGUỒN HỌC, do advisory_promoter
# ghi qua Postgres). Hai writer không biết nhau, và `advisory` còn không nằm trong
# 7 giá trị của `PlaybookDomain`.
#
# Hệ quả thật: `list_playbook_graduations()` — thứ tier_loops/capacity_loops đọc để
# đề xuất NÂNG TIER — trả về hỗn hợp hai loại bản ghi. Con số dùng để trao quyền tự
# chủ đang đếm gộp hai thứ khác bản chất.

TRACK_ADVISORY: Final = "advisory"   # học từ phán quyết người trên advisory
TRACK_PLAYBOOK: Final = "playbook"   # học từ playbook chạy có verify
TRACK_EXECUTION: Final = "execution"  # học từ mutation đã VERIFIED_SUCCESS

ALL_TRACKS: Final[tuple[str, ...]] = (TRACK_ADVISORY, TRACK_PLAYBOOK, TRACK_EXECUTION)


def split_legacy_graduation_domain(value: str | None) -> tuple[str, str]:
    """Tách giá trị `domain` cũ thành ``(domain, track)``.

    `advisory` không phải domain — nó là track, và domain của nó thật sự chưa biết.
    Trả `unknown` chứ không đoán bừa: đoán sai domain rồi dùng để cấp quyền còn tệ
    hơn thừa nhận là chưa biết.
    """
    v = (value or "").strip().lower()
    if v in ALL_TRACKS:
        return UNKNOWN, v
    return normalize_domain(v), TRACK_PLAYBOOK


__all__ = [
    "ALL_DOMAINS",
    "ALL_TRACKS",
    "APPLICATION",
    "CANONICAL_DOMAINS",
    "DATABASE",
    "HARDWARE",
    "KUBERNETES",
    "NETWORK",
    "OS_HOST",
    "SECURITY",
    "SERVICE",
    "STORAGE",
    "TRACK_ADVISORY",
    "TRACK_EXECUTION",
    "TRACK_PLAYBOOK",
    "UNKNOWN",
    "is_canonical",
    "normalize_domain",
    "require_domain",
    "split_legacy_graduation_domain",
]
