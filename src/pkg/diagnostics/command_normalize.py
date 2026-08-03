"""Chuẩn hoá lệnh chẩn đoán ở BIÊN — không tin hình dạng output của LLM.

## Vì sao tồn tại

Model 7B sinh `commands_to_run` dạng JSON. Nó **thường xuyên** nhồi cả dòng lệnh
vào một phần tử của `args`, vì trong đầu nó đó là một chuỗi shell. Đo thật
(session `omni:diag:session:ra-da66cac8746b`, 2026-08-02):

    {"command": "ps", "args": ["aux --sort=-%cpu"]}
    → execve("ps", ["aux --sort=-%cpu"])
    → error: unsupported option (BSD syntax), rc=1, stdout rỗng

Lệnh không chạy ⇒ vòng chẩn đoán mất lượt ⇒ LLM kết luận từ chỗ khác. Đây là
lớp bug "ghép theo vị trí / tin hình dạng dữ liệu ngoài" đã trả giá nhiều lần —
xem memory `project_positional_pairing_bug_class`.

Cùng session: `top` được gọi KHÔNG cờ. Ngoài tty, `top` tương tác thoát ngay với
rc=1 và không in gì. Catalogue đã ghi chú "chỉ dùng batch mode (`-b -n1`)" nhưng
ghi chú không cưỡng chế được gì — nên cưỡng chế ở đây.

## Nguyên tắc

- **Chuẩn hoá TRƯỚC khi validate.** Mọi token sinh ra vẫn đi qua
  `pkg.diagnostics.validator.validate_command` y như cũ: quét metachar,
  `WRITE_VERBS`, `deny_flags`, phạm vi đường dẫn. Tách chuỗi KHÔNG nới guard
  nào — nó chỉ làm đối số **chi tiết hơn** cho cùng bộ luật.
- **Tách có điều kiện, không tách bừa.** Đối số hợp lệ vẫn có thể chứa khoảng
  trắng: script `awk`, câu SQL sau `-e`, `--since "1 hour ago"`. Chỉ tách khi
  chuỗi có ít nhất một token **trông như cờ** (`-x`, `--long`) — dấu hiệu duy
  nhất phân biệt "một dòng lệnh bị nhồi" với "một giá trị có khoảng trắng".
- **Thuần, đồng bộ, không I/O.** Gọi được ở cả ba nơi: producer (diagnosis
  loop), gateway (fail-fast trước khi enqueue), agent (cưỡng chế cuối).
"""

from __future__ import annotations

import shlex

# Script của các lệnh này là một NGÔN NGỮ, không phải dòng đối số — không bao giờ tách.
_SCRIPT_COMMANDS: frozenset[str] = frozenset({"awk", "gawk", "mawk", "nawk", "busybox-awk"})

# Cờ mà token NGAY SAU nó là một câu lệnh DB / mẫu tìm kiếm — giá trị, không phải dòng lệnh.
_VALUE_FLAGS: frozenset[str] = frozenset({
    "-e", "--execute", "-c", "--command", "--eval", "--since", "--until", "--grep",
    "-o", "--format", "--sort", "--fields",
})

# `top` không cờ = chế độ tương tác. Ngoài tty nó thoát ngay với rc=1 và không in gì.
_TOP_BATCH_ITERATIONS = "1"


def _looks_like_flag(token: str) -> bool:
    return len(token) > 1 and token.startswith("-")


def _split_packed(arg: str) -> list[str] | None:
    """Tách một đối số bị nhồi cả dòng lệnh. ``None`` = không nên tách.

    Điều kiện tách (phải đủ cả ba):
      1. có khoảng trắng
      2. tách ra > 1 token
      3. có ít nhất một token trông như cờ

    Điều kiện (3) là thứ giữ `--since "1 hour ago"` và `SHOW STATUS` nguyên vẹn.
    """
    if not arg or not arg.strip() or arg.strip() == arg.strip().split()[0]:
        return None
    try:
        tokens = shlex.split(arg)
    except ValueError:
        # Nháy không cân — shlex ném. Rơi về tách theo khoảng trắng thuần: thà
        # chi tiết hơn còn hơn giao nguyên chuỗi hỏng cho execve.
        tokens = arg.split()
    if len(tokens) <= 1 or not any(_looks_like_flag(t) for t in tokens):
        return None
    return [t for t in tokens if t]


def _normalize_top(args: list[str]) -> list[str]:
    """`top` PHẢI ở batch mode: `-b` (không tty) + `-n <N>` (thoát sau N vòng).

    Thiếu `-b` là rc=1 im lặng; thiếu `-n` là treo tới timeout. Catalogue chỉ
    *ghi chú* điều này — ghi chú không chạy được, nên cưỡng chế ở đây.
    """
    out = list(args)
    if "-b" not in out and "--batch-mode" not in out:
        out.insert(0, "-b")
    if not any(a == "-n" or a.startswith("-n") and a[2:].isdigit() for a in out):
        out += ["-n", _TOP_BATCH_ITERATIONS]
    return out


_PER_COMMAND_NORMALIZERS = {"top": _normalize_top}


def normalize_command(command: str, args: list[str] | None = None) -> tuple[str, list[str]]:
    """Trả về ``(command, args)`` đã chuẩn hoá. Thuần — không sửa đầu vào.

    Ba việc, theo đúng thứ tự:
      1. `command` chứa khoảng trắng (`"systemctl is-failed"`) → token đầu là
         lệnh, phần còn lại ghép vào đầu `args`.
      2. Đối số bị nhồi cả dòng lệnh → tách (xem `_split_packed`).
      3. Chuẩn hoá riêng theo lệnh (hiện chỉ `top` → batch mode).
    """
    raw_cmd = (command or "").strip()
    argv = [str(a) for a in (args or [])]

    if " " in raw_cmd or "\t" in raw_cmd:
        head, *rest = raw_cmd.split()
        raw_cmd, argv = head, [*rest, *argv]

    base = raw_cmd.lstrip("/").split("/")[-1].lower()

    if base in _SCRIPT_COMMANDS:
        # Script awk: đối số nào cũng có thể là chương trình. Không đụng vào.
        normalized = argv
    else:
        normalized = []
        skip_next = False
        for arg in argv:
            if skip_next:
                # Giá trị của một cờ value-flag — là dữ liệu, không phải dòng lệnh.
                skip_next = False
                normalized.append(arg)
                continue
            if arg.split("=", 1)[0].lower() in _VALUE_FLAGS and "=" not in arg:
                skip_next = True
                normalized.append(arg)
                continue
            parts = _split_packed(arg)
            normalized.extend(parts if parts is not None else [arg])

    normalizer = _PER_COMMAND_NORMALIZERS.get(base)
    if normalizer is not None:
        normalized = normalizer(normalized)

    return raw_cmd, normalized


__all__ = ["normalize_command"]
