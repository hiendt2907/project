"""Kho điều phối CỤC BỘ trên host — thay Redis cho lease/ledger phía agent.

Vì sao tồn tại, đo được trên UAT 2026-08-11 (Đ52): lệnh tự khắc phục
``systemd.restart_unit`` tới được agent (``state=QUEUED http=200``, agent chạy đủ
``accept→progress→terminal``) nhưng thất bại với::

    {"rc": 1, "reason": "executor_exception: Timeout connecting to server"}

``run.env`` trên cả 3 VM khách trỏ
``AOIP_REDIS_URL=redis://redis.multi-agent.svc.cluster.local:6379/0`` — tên DNS **chỉ**
phân giải bên trong k3s. Từ VM khách `getent hosts` không ra, kết nối treo tới timeout.
Redis service là ClusterIP, cố ý không có đường ra ngoài — và không nên có.

Vì sao CỤC BỘ mới đúng, chứ không phải mở Redis ra ngoài hay xây API điều phối:
``ExecutionLease``/``IdempotencyLedger`` chỉ có call site trong ``aoip/agent/*`` (không
chỗ nào phía Omni dùng), và lease scope là ``{tenant}:{unit-systemd}`` — writer luôn là
agent TRÊN CHÍNH HOST đó. Không tồn tại nhu cầu điều phối liên máy. Bắt VM khách nối vào
Redis nội bộ của Omni còn vi phạm ranh giới NÃO/THÂN (CLAUDE.md): agent là chân tay trên
hạ tầng khách hàng, không phải một thành phần của cluster Omni. Mở Redis ra ngoài sẽ phơi
kho dữ liệu lõi của MỌI tenant cho từng VM khách — một agent bị chiếm là đọc/ghi được tất cả.

Chủ ý giữ đúng bề mặt Redis mà hai file kia dùng (``set``/``get``/``delete``/``eval``) để
``lease.py`` và ``idempotency.py`` **KHÔNG đổi một dòng nào** — chúng là code an toàn đã
có test, đổi chúng là rủi ro không cần thiết. Ở đây chỉ đổi *chỗ cất dữ liệu*, không đổi
*ngữ nghĩa*.

Đồng thời chặt hơn Redis ở một điểm: khoá ``flock`` trên chính file nên hai tiến trình
cùng host vẫn serialize đúng kể cả khi mất mạng hoàn toàn (ca dual-agent Đ50 từng có 2
process cùng chạy trên một máy).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
from typing import Any

from aoip.agent.lease import _RELEASE_SCRIPT, _RENEW_SCRIPT

_DEFAULT_PATH = "/var/lib/omni-agent/coord.json"


class LocalCoordStore:
    """Kho key-value có TTL, bền qua restart, an toàn giữa các tiến trình cùng host.

    Async API khớp tập con Redis mà ``ExecutionLease``/``IdempotencyLedger`` gọi tới.
    Mọi thao tác đọc-sửa-ghi chạy dưới ``flock`` độc quyền nên là nguyên tử với mọi tiến
    trình khác trên máy — đó là điều kiện để ``SET NX`` và hai script CAS giữ đúng nghĩa.
    """

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = path
        self._lock = asyncio.Lock()   # nối tiếp trong CÙNG tiến trình; flock lo liên tiến trình

    # ── nội bộ ──────────────────────────────────────────────────────────────

    def _load_locked(self, fh) -> dict[str, Any]:
        """Đọc + dọn key hết hạn. File hỏng ⇒ coi như rỗng.

        Fail-open có chủ đích: đĩa đầy hoặc bị kill giữa lúc ghi có thể để lại JSON dở.
        Nổ ngược vào agent sẽ làm chết luôn đường thu thập bằng chứng — tệ hơn nhiều so
        với việc mất trí nhớ lease (lease mất thì cùng lắm chạy lại một lệnh đã guard).
        """
        try:
            fh.seek(0)
            raw = fh.read()
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                data = {}
        except (ValueError, OSError):
            data = {}
        now = time.time()
        return {
            k: v for k, v in data.items()
            if isinstance(v, dict) and (v.get("exp") is None or v["exp"] > now)
        }

    def _write_locked(self, fh, data: dict[str, Any]) -> None:
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(data))
        fh.flush()
        os.fsync(fh.fileno())

    def _mutate(self, fn):
        """Chạy `fn(data)` dưới flock độc quyền; ghi lại nếu fn báo có đổi."""
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                data = self._load_locked(fh)
                result, changed = fn(data)
                if changed:
                    self._write_locked(fh, data)
                return result
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    async def _run(self, fn):
        async with self._lock:
            return await asyncio.to_thread(self._mutate, fn)

    # ── bề mặt Redis ────────────────────────────────────────────────────────

    async def set(
        self, key: str, value: str, *, nx: bool = False, ex: int | None = None,
    ) -> bool | None:
        """`SET key value [NX] [EX]`. Trả True nếu ghi; None nếu NX và key đã tồn tại.

        Trả `None` (không phải False) khi NX trượt — đúng như redis-py, vì
        ``ExecutionLease.acquire`` kiểm ``if not ok`` và ``IdempotencyLedger.claim`` bọc
        ``bool(...)``.
        """
        def _fn(data):
            if nx and key in data:
                return None, False
            data[key] = {"v": value, "exp": (time.time() + ex) if ex else None}
            return True, True
        return await self._run(_fn)

    async def get(self, key: str) -> str | None:
        def _fn(data):
            entry = data.get(key)
            return (entry["v"] if entry else None), False
        return await self._run(_fn)

    async def delete(self, key: str) -> int:
        def _fn(data):
            if key in data:
                del data[key]
                return 1, True
            return 0, False
        return await self._run(_fn)

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        """Hai script CAS của ``lease.py``, thực hiện nguyên tử dưới flock.

        Cố ý so khớp theo NỘI DUNG script và NỔ khi không nhận ra, thay vì trả 0 cho
        script lạ: nếu `lease.py` đổi script mà quên đổi ở đây, im lặng trả 0 sẽ khiến
        renew luôn thất bại ⇒ mọi recovery escalate mà không ai hiểu vì sao.
        """
        key = str(args[0])
        token = str(args[1])

        if script == _RENEW_SCRIPT:
            ttl = int(args[2])

            def _renew(data):
                entry = data.get(key)
                if entry and entry["v"] == token:
                    entry["exp"] = time.time() + ttl
                    return 1, True
                return 0, False
            return await self._run(_renew)

        if script == _RELEASE_SCRIPT:
            def _release(data):
                entry = data.get(key)
                if entry and entry["v"] == token:
                    del data[key]
                    return 1, True
                return 0, False
            return await self._run(_release)

        raise NotImplementedError(
            "LocalCoordStore chỉ hỗ trợ script CAS của aoip.agent.lease — "
            "script lạ phải nổ, không được âm thầm trả 0"
        )
