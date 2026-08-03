"""INV_DIAG_MEASURED — kết luận không được nêu đại lượng CHƯA AI ĐO.

## Vì sao tồn tại (lỗi thật, 2026-08-02)

`_apply_grounding_gate` (services/analyst/diagnosis_loop.py) và
`apply_advisory_grounding_gate` (workers/advisory_grounding_gate.py) đều bắt
claim theo **token neo được**: đường dẫn tuyệt đối, phần trăm, tên object. Cả
hai đều mù trước câu này:

    "Insufficient memory available on the host"

Không đường dẫn, không phần trăm, không tên object ⇒ lọt sạch. Nhưng nó là kết
luận về BỘ NHỚ trong một phiên mà lệnh duy nhất chạy được là `df -h` (ĐĨA).
Session `omni:diag:session:ra-689e6dc59ea4`:

    alert     [cust-app] CPU 98.3%>80.0%
    turn 1    "CPU saturation on host cust-app"  conf 0.75   → xin chạy `df -h`
    turn 2    df trả 18% used → "đĩa ổn nên chắc do bộ nhớ"
              → vứt giả thuyết ĐÚNG, conf 0.75 → 0.95, diagnosis_complete

Ba lỗi cùng lúc: (a) loại trừ một khả năng được dùng làm bằng chứng cho khả
năng khác; (b) confidence TĂNG sau khi bằng chứng bị loại; (c) kết luận về đại
lượng chưa từng đo.

Module này bổ sung một trục kiểm hoàn toàn khác hai gate cũ: không hỏi "chuỗi
này có trong evidence không" mà hỏi **"có công cụ nào trong phiên này ĐO đại
lượng đó không"**. Hai trục bù nhau, không thay nhau — diagnosis_loop chạy cả hai.

## Ba phép kiểm

1. `unmeasured_quantities` — đại lượng nêu trong kết luận phải nằm trong
   (đại lượng ALERT nói tới) ∪ (đại lượng các lệnh ĐÃ CHẠY THÀNH CÔNG đo được).
2. `contradicted_service_claims` — kết luận "unit X hỏng/crash" trong khi
   `systemctl is-failed` / `is-active` của phiên nói ngược lại.
3. `confidence_inflation` — confidence tăng ở lượt LLM đổi sang đại lượng mới
   mà kết quả lệnh vừa nhận KHÔNG đo đại lượng đó.

## Vì sao KHÔNG tính "facts" là đã đo

Mỗi cảnh báo remote-agent đều kèm một mẫu metric đầy đủ (cpu/mem/disk luôn có
mặt). Nếu coi facts là "đã đo" thì phép kiểm rỗng nghĩa — mọi đại lượng luôn
grounded, kể cả cái LLM bịa. `alert_hint` mới là thứ nói đại lượng nào ĐANG bất
thường. Kết luận rẽ sang một đại lượng khác thì phải có lệnh đo nó — đúng bằng
hành vi ta muốn dạy model. Chẩn đoán "đĩa đầy" từ alert đĩa vẫn qua bình thường.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

# ── Đại lượng canonical ───────────────────────────────────────────────────────

CPU = "cpu"
MEMORY = "memory"
DISK = "disk"
INODE = "inode"
NETWORK = "network"
SERVICE = "service"

QUANTITIES: frozenset[str] = frozenset({CPU, MEMORY, DISK, INODE, NETWORK, SERVICE})

# Trần confidence khi kết luận bị vô hiệu hoá. Bằng `_UNGROUNDED_CONFIDENCE_CAP`
# của diagnosis_loop — cùng nghĩa "đừng tin cái này", giữ một con số duy nhất.
NEUTRALIZED_CONFIDENCE_CAP = 0.3

# ── Nhận diện CLAIM trong kết luận ────────────────────────────────────────────
#
# Neo hẹp có chủ đích. `service`/`network` KHÔNG bắt theo danh từ trần: "caused by
# the nginx service" chỉ nêu TÊN dịch vụ, không khẳng định trạng thái — bắt nó là
# bắt oan gần như mọi kết luận. Chỉ khẳng định về TRẠNG THÁI mới là claim cần đo.

_FAIL_WORDS = r"crash|fail|down|dead|inactive|not running|stopp?ed|restart loop|flapping|hỏng|sập|chết"

_CLAIM_PATTERNS: dict[str, re.Pattern[str]] = {
    CPU: re.compile(r"\bcpu\b|\bload[ _]av|\bprocessor\b|\bthrottl|\bsaturation\b", re.I),
    MEMORY: re.compile(
        r"\bmemory\b|\bmem[_ ](?:percent|used|available|pressure)|\bram\b|\boom\b|"
        r"out of memory|\bswap\b|bộ nhớ",
        re.I,
    ),
    DISK: re.compile(
        r"\bdisk\b|\bfilesystem\b|\bpartition\b|no space left|out of (?:disk )?space|"
        r"storage (?:is )?full|\bdu\b -sh|\bđĩa\b|dung lượng",
        re.I,
    ),
    INODE: re.compile(r"\binode", re.I),
    NETWORK: re.compile(
        rf"\bnetwork\b[^.]{{0,40}}(?:{_FAIL_WORDS}|issue|problem|latency|loss|congest|saturat)|"
        r"packet loss|connection (?:refused|timeout|reset)|"
        r"\bport\b[^.]{0,30}(?:closed|not listening|refused|unreachable)|"
        r"\bdns\b[^.]{0,30}(?:fail|resolution|timeout)|\blistener\b[^.]{0,30}(?:lost|gone|down)",
        re.I,
    ),
    SERVICE: re.compile(
        rf"(?:service|unit|daemon|\.service)\b[^.]{{0,40}}(?:{_FAIL_WORDS})|"
        rf"(?:{_FAIL_WORDS})[^.]{{0,30}}(?:service|unit|daemon)\b|"
        rf"[\w.@-]+\.service\b[^.]{{0,40}}(?:{_FAIL_WORDS})",
        re.I,
    ),
}

# Alert nói đại lượng nào. Cùng bộ mẫu nhưng nới cho `service`/`network` vì
# alert_hint là văn bản máy sinh, ngắn, không có mệnh đề trạng thái.
_ALERT_PATTERNS: dict[str, re.Pattern[str]] = {
    **_CLAIM_PATTERNS,
    SERVICE: re.compile(r"\bservice\b|\bunit\b|\.service\b|failed_units|systemd", re.I),
    NETWORK: re.compile(r"\bnetwork\b|\bport\b|\blistener\b|\bsocket\b|\bdns\b|packet", re.I),
}

# ── Lệnh nào ĐO đại lượng nào ─────────────────────────────────────────────────
#
# Tra theo BASENAME của lệnh đã chạy. Chỉ liệt kê thứ lệnh đó thật sự in ra —
# `df` không nói gì về bộ nhớ, `free` không nói gì về đĩa. Khai rộng ở đây là
# tự vô hiệu hoá chính cái gate này.

_COMMAND_MEASURES: dict[str, frozenset[str]] = {
    # đĩa
    "df": frozenset({DISK}),
    "du": frozenset({DISK}),
    "lsblk": frozenset({DISK}),
    "findmnt": frozenset({DISK}),
    "blkid": frozenset({DISK}),
    "mount": frozenset({DISK}),
    "ls": frozenset({DISK}),
    "stat": frozenset({DISK}),
    # bộ nhớ
    "free": frozenset({MEMORY}),
    "vmstat": frozenset({MEMORY, CPU}),
    "swapon": frozenset({MEMORY}),
    "slabtop": frozenset({MEMORY}),
    "dmesg": frozenset({MEMORY}),  # OOM killer sống ở đây
    # cpu
    "top": frozenset({CPU, MEMORY}),
    "htop": frozenset({CPU, MEMORY}),
    "ps": frozenset({CPU, MEMORY}),  # %CPU và %MEM cùng một bảng
    "pidstat": frozenset({CPU, MEMORY}),
    "mpstat": frozenset({CPU}),
    "uptime": frozenset({CPU}),
    "sar": frozenset({CPU, MEMORY, DISK, NETWORK}),
    "iostat": frozenset({CPU, DISK}),
    "nproc": frozenset({CPU}),
    # mạng
    "ss": frozenset({NETWORK}),
    "netstat": frozenset({NETWORK}),
    "ip": frozenset({NETWORK}),
    "ping": frozenset({NETWORK}),
    "traceroute": frozenset({NETWORK}),
    "dig": frozenset({NETWORK}),
    "host": frozenset({NETWORK}),
    "nslookup": frozenset({NETWORK}),
    "curl": frozenset({NETWORK}),
    "nc": frozenset({NETWORK}),
    "ethtool": frozenset({NETWORK}),
    # dịch vụ
    "systemctl": frozenset({SERVICE}),
    "journalctl": frozenset({SERVICE}),
    "service": frozenset({SERVICE}),
    "initctl": frozenset({SERVICE}),
    "supervisorctl": frozenset({SERVICE}),
}


def _basename(command_str: str) -> str:
    head = (command_str or "").strip().split()
    if not head:
        return ""
    return head[0].lstrip("/").split("/")[-1].lower()


def quantities_claimed(text: str) -> set[str]:
    """Đại lượng mà ``text`` KHẲNG ĐỊNH là nguyên nhân/trạng thái."""
    body = text or ""
    return {q for q, pat in _CLAIM_PATTERNS.items() if pat.search(body)}


def quantities_in_alert(alert_hint: str) -> set[str]:
    """Đại lượng mà cảnh báo gốc nói tới — nguồn grounding không cần lệnh."""
    body = alert_hint or ""
    return {q for q, pat in _ALERT_PATTERNS.items() if pat.search(body)}


def quantities_measured(command_strings: Iterable[str]) -> set[str]:
    """Đại lượng các lệnh này thực sự đo được.

    `df -i` là ngoại lệ duy nhất cần đọc cờ: nó đo INODE chứ không phải byte —
    và đúng chỗ này từng sinh claim "inode exhaustion confirmed" không có `df -i`
    nào trong phiên (2026-07-13).
    """
    out: set[str] = set()
    for cmd in command_strings:
        base = _basename(cmd)
        measured = _COMMAND_MEASURES.get(base)
        if not measured:
            continue
        if base == "df":
            tokens = (cmd or "").split()
            has_i = any(t == "-i" or (t.startswith("-") and not t.startswith("--") and "i" in t[1:])
                        for t in tokens[1:])
            out |= ({INODE} if has_i else set()) | {DISK}
            continue
        out |= measured
    return out


def successful_command_strings(command_results: Iterable[dict[str, Any]]) -> list[str]:
    """Chỉ lệnh CHẠY ĐƯỢC mới tính là đã đo.

    `ps aux --sort=-%cpu` rc=1 (BSD syntax) không đo được gì; coi nó là bằng
    chứng là quay lại đúng lỗi ban đầu. Cũng loại `blocked` và `timeout`.
    """
    out: list[str] = []
    for r in command_results or []:
        if not isinstance(r, dict):
            continue
        if r.get("blocked") or r.get("status") == "timeout":
            continue
        if int(r.get("rc", 0) or 0) != 0:
            continue
        cmd = str(r.get("command_str") or "").strip()
        if cmd:
            out.append(cmd)
    return out


def unmeasured_quantities(
    root_cause: str, alert_hint: str, command_strings: Iterable[str]
) -> list[str]:
    """Đại lượng nêu trong kết luận mà KHÔNG alert nào nói tới, KHÔNG lệnh nào đo.

    Trả `[]` khi không có nguồn grounding nào (alert rỗng **và** không lệnh nào
    chạy) — phiên degraded kết luận thuần từ facts là hợp lệ, và đoán bừa ở đây
    sẽ vô hiệu hoá mọi chẩn đoán offline.
    """
    claimed = quantities_claimed(root_cause)
    if not claimed:
        return []
    grounded = quantities_in_alert(alert_hint) | quantities_measured(command_strings)
    if not grounded:
        return []
    return sorted(claimed - grounded)


# ── Kiểm mâu thuẫn: kết luận "unit hỏng" vs bằng chứng "unit khoẻ" ────────────

_HEALTHY_STATES = frozenset({"active", "running", "activating", "reloading"})
_UNIT_RE = re.compile(r"\b([\w.@-]+\.(?:service|socket|timer|target|mount))\b", re.I)
_SERVICE_FAIL_CLAIM_RE = re.compile(_FAIL_WORDS, re.I)


def _service_health_signals(command_results: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    """(unit-hoặc-'' , mô tả bằng chứng) cho mỗi phép đo cho thấy KHOẺ.

    Ngữ nghĩa systemd, không phải quy ước của ta:
      * `systemctl is-failed [unit]` → **rc=0 nghĩa là ĐANG HỎNG**. rc≠0 là khoẻ.
        Không có unit ⇒ hỏi trạng thái CẢ HỆ; rc≠0 ⇒ không unit nào hỏng.
      * `systemctl is-active <unit>` → stdout `active` là khoẻ.
    """
    signals: list[tuple[str, str]] = []
    for r in command_results or []:
        if not isinstance(r, dict) or r.get("blocked") or r.get("status") == "timeout":
            continue
        cmd = str(r.get("command_str") or "").strip()
        tokens = cmd.split()
        if len(tokens) < 2 or _basename(cmd) != "systemctl":
            continue
        sub = tokens[1].lower()
        if sub not in ("is-failed", "is-active"):
            continue
        rc = int(r.get("rc", 0) or 0)
        stdout = str(r.get("stdout") or "").strip().lower()
        units = [t for t in tokens[2:] if not t.startswith("-")]
        target = units[0] if units else ""
        if sub == "is-failed" and rc != 0:
            signals.append((target, f"`{cmd}` rc={rc} ⇒ systemd báo KHÔNG hỏng"))
        elif sub == "is-active" and (rc == 0 or stdout in _HEALTHY_STATES):
            if stdout in _HEALTHY_STATES or rc == 0:
                signals.append((target, f"`{cmd}` → {stdout or 'active'} ⇒ đang chạy"))
    return signals


def contradicted_service_claims(
    root_cause: str, command_results: Iterable[dict[str, Any]]
) -> list[str]:
    """Kết luận nói unit hỏng, nhưng phép đo trong CHÍNH phiên này nói ngược lại.

    Ca thật `ra-d645c49ed6d1`: kết luận "aoip-agent.service is crashing" +
    `suggested_recovery=systemd.restart_unit` trong khi `systemctl is-failed`
    trả rc=1 (không unit nào hỏng) và journalctl trưng log cách đó ba tuần.
    Đây là đường ngắn nhất tới một lần restart tự động vô cớ.

    Đánh đổi có ý thức: một unit crash-loop nhưng systemd restart lại thành công
    vẫn đọc là "active" ⇒ có thể bắt oan. Nên hậu quả cố tình nhẹ — hạ độ tin và
    gỡ auto-recovery, KHÔNG xoá kết luận, KHÔNG chặn thẻ. Restart nhầm một unit
    đang khoẻ đắt hơn một thẻ ghi "độ tin thấp".
    """
    rc_text = root_cause or ""
    if SERVICE not in quantities_claimed(rc_text):
        return []
    if not _SERVICE_FAIL_CLAIM_RE.search(rc_text):
        return []
    claimed_units = {u.lower() for u in _UNIT_RE.findall(rc_text)}
    out: list[str] = []
    for target, evidence in _service_health_signals(command_results):
        if not target:
            # `systemctl is-failed` không tham số: phán về TOÀN hệ ⇒ phủ mọi unit.
            out.append(evidence)
        elif target.lower() in claimed_units or any(
            target.lower().startswith(u.split(".")[0]) for u in claimed_units
        ):
            out.append(evidence)
    return out


# ── Kiểm confidence tăng vô căn cứ ────────────────────────────────────────────


def confidence_inflation(turns: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Lượt nào confidence TĂNG khi đổi sang đại lượng chưa có phép đo nào?

    Bằng chứng "mới" của lượt N là kết quả lệnh mà lượt N-1 đã xin (mô hình lưu
    kết quả vào bản ghi của lượt ĐÃ XIN, không phải lượt đọc chúng). Nên phép so
    là: đại lượng mới xuất hiện ở giả thuyết lượt N có được các lệnh của lượt
    N-1 đo không.

    Ca thật: 0.75 (cpu) → 0.95 (memory) sau khi chạy `df` — `df` không đo bộ nhớ.
    """
    for prev, cur in zip(turns or [], (turns or [])[1:]):
        prev_conf = float(prev.get("confidence", 0.0) or 0.0)
        cur_conf = float(cur.get("confidence", 0.0) or 0.0)
        if cur_conf <= prev_conf:
            continue
        prev_q = quantities_claimed(str(prev.get("hypothesis", "")))
        cur_q = quantities_claimed(str(cur.get("hypothesis", "")))
        new_q = cur_q - prev_q
        if not new_q:
            continue
        measured = quantities_measured(
            successful_command_strings(prev.get("command_results", []))
        )
        unsupported = sorted(new_q - measured)
        if unsupported:
            return {
                "turn": cur.get("turn"),
                "from_confidence": prev_conf,
                "to_confidence": cur_conf,
                "unsupported_quantities": unsupported,
            }
    return None


# ── Cổng hợp nhất ─────────────────────────────────────────────────────────────

_UNMEASURED_PREFIX = "[UNMEASURED: {claims}] "
_CONTRADICTED_PREFIX = "[CONTRADICTED: {evidence}] "


def apply_measurement_gate(
    final: dict[str, Any],
    *,
    alert_hint: str = "",
    command_results: list[dict[str, Any]] | None = None,
    turns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Trả về dict MỚI với kết luận đã bị vô hiệu hoá nếu vi phạm. Không sửa đầu vào.

    Cố ý KHÔNG chặn thẻ Telegram: cảnh báo gốc (CPU 98%) vẫn là sự cố thật và
    người vận hành cần thấy nó. Cái bị vô hiệu hoá là **câu kết luận** — đánh
    dấu, hạ trần confidence, và gỡ `suggested_recovery` (đường tới một mutation
    tự động dựa trên claim không đo được).
    """
    results = list(command_results or [])
    root_cause = str(final.get("root_cause", "") or "")

    unmeasured = unmeasured_quantities(
        root_cause, alert_hint, successful_command_strings(results)
    )
    contradicted = contradicted_service_claims(root_cause, results)
    inflation = confidence_inflation(turns or [])

    if not unmeasured and not contradicted and inflation is None:
        return dict(final)

    out = dict(final)
    prefix = ""
    if unmeasured:
        out["unmeasured_quantities"] = unmeasured
        prefix += _UNMEASURED_PREFIX.format(claims=", ".join(unmeasured))
    if contradicted:
        out["contradicted_claims"] = contradicted
        prefix += _CONTRADICTED_PREFIX.format(evidence="; ".join(contradicted)[:200])
    if inflation is not None:
        out["confidence_inflation"] = inflation

    if prefix:
        out["root_cause"] = f"{prefix}{root_cause}"
        out["suggested_recovery"] = None

    current = float(final.get("confidence", 0.0) or 0.0)
    cap = current
    if unmeasured or contradicted:
        cap = min(cap, NEUTRALIZED_CONFIDENCE_CAP)
    if inflation is not None:
        # Trần = mức tin TRƯỚC lần tăng vô căn cứ. Không phạt phần đã có căn cứ.
        cap = min(cap, float(inflation["from_confidence"]))
    out["confidence"] = cap
    logger.warning(
        "event=measurement_grounding_gate_fired unmeasured=%s contradicted=%d "
        "inflation=%s confidence=%.2f→%.2f",
        unmeasured, len(contradicted), bool(inflation), current, cap,
    )
    return out


__all__ = [
    "CPU",
    "DISK",
    "INODE",
    "MEMORY",
    "NETWORK",
    "NEUTRALIZED_CONFIDENCE_CAP",
    "QUANTITIES",
    "SERVICE",
    "apply_measurement_gate",
    "confidence_inflation",
    "contradicted_service_claims",
    "quantities_claimed",
    "quantities_in_alert",
    "quantities_measured",
    "successful_command_strings",
    "unmeasured_quantities",
]
