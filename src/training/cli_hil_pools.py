"""Sinh 100k ngữ cảnh tiếng Việt + lệnh CLI gợi ý (tổ hợp 10^5, id ổn định theo index)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from execution.policy import check_sandbox_command

# Lệnh read-only / quan sát — không rm/dd/mkfs; dùng cho payload suggested_commands
CLI_SUGGEST_ALLOWLIST: tuple[str, ...] = (
    "kubectl top nodes",
    "kubectl top pods -A",
    "kubectl get pods -A -o wide",
    "kubectl get nodes -o wide",
    "kubectl get deployment -A -o wide",
    "kubectl get svc -A",
    "iostat -xz 1",
    "vmstat 1 5",
    "df -h",
    "free -h",
    "uptime",
    "cat /proc/loadavg",
    "ss -s",
    "kubectl get events -A --field-selector type=Warning",
    "kubectl get ns",
    "lsblk -f",
    "mpstat -P ALL 1 1",
    "sar -n DEV 1 1",
    "kubectl get pvc -A",
    "netstat -s",
)

GENERATOR_VERSION = "v1"
MAX_COMBINATIONS = 100_000  # 10^5

# 5 chiều × 10 giá trị = 10^5 tổ hợp
_OPENINGS = (
    "Đại ca",
    "Anh",
    "Cho em",
    "Team kêu",
    "User báo",
    "On-call nhắn",
    "Sếp hỏi",
    "Trong chat",
    "Theo ticket",
    "Nhóm dev",
)
_ACTIONS = (
    "kiểm tra hệ thống",
    "xem node còn khỏe không",
    "coi CPU/RAM hộ",
    "check disk và IO",
    "xem mạng có tắc không",
    "top pod/node giúp",
    "snapshot nhanh",
    "đọc health tổng quát",
    "xem có gì bất thường",
    "rà soát nhanh",
)
_CONTEXTS = (
    "sau deploy",
    "lúc incident",
    "cuối tuần",
    "giờ cao điểm",
    "baseline",
    "trước khi release",
    "khi alert fire",
    "lúc demo",
    "sau migration",
    "handoff ca",
)
_TONES = (
    "gấp nha",
    "nhẹ nhàng thôi",
    "chi tiết được thì tốt",
    "lệnh ngắn là được",
    "cần số liệu thật",
    "đừng đoán mò",
    "ưu tiên lệnh quen tay",
    "chỉ ops thôi",
    "nhớ ghi chú lại",
    "thanks trước",
)
_FOCUS = (
    "kiểu kubectl top",
    "kiểu iostat/vmstat",
    "disk và filesystem",
    "memory và load",
    "kubectl get pods",
    "kubectl get nodes",
    "uptime và loadavg",
    "socket / ss",
    "events / warning",
    "PVC/storage",
)

_TEMPLATES = (
    "{o} {act} ({ctx}, {tone}). Em gợi ý chạy `{cmd}` — góc {focus}.",
    "{o} {act} lúc {ctx}, {tone}. Thử `{cmd}` ({focus}).",
    "{o} nhờ {act} ({ctx}). {tone}. Gợi ý: `{cmd}` — {focus}.",
    "Ơ kìa {ctx}: {o} muốn {act}, {tone}. Mình hay `{cmd}` khi cần {focus}.",
    "{o} {act} — bối cảnh {ctx}, {tone}. `{cmd}` ổn cho tình huống {focus}.",
    "{ctx} mà {o} kêu {act}, {tone}. Coi `{cmd}` trước ({focus}).",
    "{o} {act}; {ctx}; {tone}. Tạm `{cmd}` — liên quan {focus}.",
    "Case {ctx}: {act}, {o} bảo {tone}. Check `{cmd}` / {focus}.",
    "{o} cần {act} ({ctx}), {tone}. Benchmark tay: `{cmd}` ({focus}).",
    "{act} giúp {o} ({ctx}), {tone}. CLI gợi ý `{cmd}` — {focus}.",
)


def _validate_allowlist() -> None:
    for cmd in CLI_SUGGEST_ALLOWLIST:
        r = check_sandbox_command(cmd, lab_unchained=False)
        if r.verdict.value != "allowed_auto":
            raise RuntimeError(f"cli_hil allowlist policy rejected: {cmd!r} -> {r.reason}")


_validate_allowlist()


@dataclass(frozen=True)
class CliHilEntry:
    index: int
    match_text: str
    suggested_commands: tuple[str, ...]
    point_id: str
    embed_text: str


def _indices_for_index(i: int) -> tuple[int, int, int, int, int]:
    if i < 0 or i >= MAX_COMBINATIONS:
        raise ValueError(f"index must be in [0, {MAX_COMBINATIONS}), got {i}")
    a = i % 10
    b = (i // 10) % 10
    c = (i // 100) % 10
    d = (i // 1000) % 10
    e = (i // 10000) % 10
    return a, b, c, d, e


def _commands_for_index(i: int) -> tuple[str, str]:
    a, b, c, d, e = _indices_for_index(i)
    n = len(CLI_SUGGEST_ALLOWLIST)
    primary_i = (a + b * 3 + c * 5 + d * 7 + e * 11) % n
    secondary_i = (primary_i + 13 + e) % n
    p = CLI_SUGGEST_ALLOWLIST[primary_i]
    s = CLI_SUGGEST_ALLOWLIST[secondary_i]
    if p == s:
        s = CLI_SUGGEST_ALLOWLIST[(secondary_i + 1) % n]
    return p, s


def cli_hil_point_id(index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cli_hil:{GENERATOR_VERSION}:{index}"))


def generate_cli_hil_entry(index: int) -> CliHilEntry:
    a, b, c, d, e = _indices_for_index(index)
    o, act, ctx, tone = _OPENINGS[a], _ACTIONS[b], _CONTEXTS[c], _TONES[d]
    focus = _FOCUS[e]
    cmd_primary, cmd_secondary = _commands_for_index(index)
    tmpl = _TEMPLATES[index % len(_TEMPLATES)]
    match_text = tmpl.format(o=o, act=act, ctx=ctx, tone=tone, cmd=cmd_primary, focus=focus)
    suggested = (cmd_primary, cmd_secondary)
    embed_text = match_text + "\n---\n" + "\n".join(suggested)
    return CliHilEntry(
        index=index,
        match_text=match_text,
        suggested_commands=suggested,
        point_id=cli_hil_point_id(index),
        embed_text=embed_text,
    )


def cli_hil_payload(entry: CliHilEntry) -> dict[str, object]:
    return {
        "kind": "cli_hil",
        "generator_version": GENERATOR_VERSION,
        "index": entry.index,
        "match_text": entry.match_text[:8000],
        "suggested_commands": list(entry.suggested_commands),
    }
