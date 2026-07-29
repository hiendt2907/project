"""Cửa duy nhất để collector chạy lệnh — cùng validator với executor và gateway.

Trước file này, `collectors/*.py` + `discovery.py` + `pkg_origin.py` gọi
`asyncio.create_subprocess_exec` TRỰC TIẾP, không qua validator nào. Hệ quả đo được:
chúng đang chạy `cat` — chính lệnh nằm trong `_CONTENT_READ_BLOCKED` của executor. Tức
chính sách "metadata-only" chưa bao giờ đúng trên đường collector; nó chỉ đúng trên
đường command-channel.

Nên guard này không phải lớp bảo vệ thêm cho vui: nó là chỗ chính sách bắt đầu áp lên
đường thực thi ĐÔNG NHẤT của agent.

Bị chặn ⇒ collector nhận rc≠0 và tự xử lý như một lệnh thất bại (mọi `_run` đều
"never raises" và mọi caller đều đã có nhánh rc≠0). Cố tình KHÔNG ném exception: một
entry catalogue thiếu không được biến thành agent crash trên hạ tầng khách hàng.
"""

from __future__ import annotations

import logging

from pkg.diagnostics.validator import validate_command

logger = logging.getLogger(__name__)


def check(cmd: list[str]) -> str:
    """Trả "" nếu được phép, ngược lại trả lý do (đã log WARNING)."""
    if not cmd:
        return "empty_command"
    ok, reason = validate_command(cmd[0], list(cmd[1:]))
    if ok:
        return ""
    logger.warning(
        "[exec-guard] BLOCKED collector command=%s args=%s reason=%s",
        cmd[0], cmd[1:6], reason,
    )
    return reason


__all__ = ["check"]
