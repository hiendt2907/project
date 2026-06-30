"""CommandTransport — seam thực thi lệnh, tách KIẾN TRÚC khỏi MÔI TRƯỜNG.

Vì sao tồn tại: Remote Agent KHÔNG được biết mình đang chạy ở đâu (OrbStack lab,
EC2, bare-metal, VMware…). Discovery logic Linux (ss/systemctl/env) là một; chỉ
CÁCH chạy lệnh khác nhau. Transport là điểm cắm đó:

  - ``LocalTransport``  — agent cài THẲNG trên host (deployment thật, phổ biến nhất).
  - ``SSHTransport``    — chạy lệnh qua SSH tới host từ xa (agentless / bootstrap).
  - ``OrbTransport``    — CHỈ DÙNG TRONG LAB: bọc ``orb -m <vm>``. Không phải dependency
                          kiến trúc; deploy thật không bao giờ thấy lớp này.

Đổi môi trường = đổi transport, KHÔNG đổi một dòng discovery. Đó là bằng chứng
OrbStack chỉ là lab.
"""
from __future__ import annotations

import asyncio
from typing import Protocol


class CommandTransport(Protocol):
    target: str  # định danh host (dùng cho agent identity / scope)

    async def run(self, argv: list[str], *, timeout: float = 15.0) -> tuple[str, int]: ...


async def _exec(argv: list[str], timeout: float) -> tuple[str, int]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode(errors="replace"), proc.returncode or 0
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return "", 1


class LocalTransport:
    """Chạy lệnh ngay trên host này (agent đã cài tại chỗ)."""

    def __init__(self, target: str = "localhost") -> None:
        self.target = target

    async def run(self, argv: list[str], *, timeout: float = 15.0) -> tuple[str, int]:
        return await _exec(list(argv), timeout)


class SSHTransport:
    """Chạy lệnh trên host từ xa qua SSH (deployment thật, không cần cài trước)."""

    def __init__(self, host: str, *, user: str | None = None, ssh_opts: list[str] | None = None) -> None:
        self.target = host
        self._dest = f"{user}@{host}" if user else host
        self._opts = ssh_opts or ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]

    async def run(self, argv: list[str], *, timeout: float = 15.0) -> tuple[str, int]:
        return await _exec(["ssh", *self._opts, self._dest, *argv], timeout)


class OrbTransport:
    """LAB ONLY — bọc ``orb -m <vm>``. KHÔNG phải dependency kiến trúc.

    Tồn tại chỉ để chạy/kiểm thử RemoteLinuxBackend trong môi trường OrbStack. Code
    discovery không biết gì về lớp này; deploy thật thay bằng Local/SSH transport.
    """

    def __init__(self, vm: str) -> None:
        self.target = vm
        self._vm = vm

    async def run(self, argv: list[str], *, timeout: float = 15.0) -> tuple[str, int]:
        return await _exec(["orb", "-m", self._vm, *argv], timeout)
