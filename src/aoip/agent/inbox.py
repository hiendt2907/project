"""Local durable inbox — agent side. Persist TRƯỚC khi execute; resume sau restart.

Vì sao tồn tại: agent nhận command qua kênh durable (Gateway) nhưng bản thân agent có
thể CRASH hoặc REBOOT giữa chừng. Nếu chỉ giữ command trong RAM, restart = mất tiến độ,
hoặc tệ hơn: mù mờ về việc mutation đã chạy chưa → blind retry = mutation lặp.

Inbox này ghi command xuống đĩa cục bộ (atomic) trước khi làm bất cứ gì, và tiến hoá
một local state riêng biệt với delivery-state của Gateway:

    RECEIVED → ACCEPTED → RUNNING → OUTCOME_RECORDED → REPORTED → ACKED

Bất biến an toàn:
- Persist RECEIVED **trước** khi execute. Restart thấy RECEIVED/ACCEPTED (chưa OUTCOME) →
  an toàn (chưa chắc mutation chạy) nhưng PHẢI qua idempotency ledger, KHÔNG blind retry.
- OUTCOME_RECORDED nhưng chưa REPORTED/ACKED → chỉ **re-report** (mutation đã xong, KHÔNG
  chạy lại). Đây là case "crash sau mutation trước report".
- Chỉ ARCHIVE (xoá) khi có terminal acknowledgement từ Gateway (state=ACKED).

Lưu JSON-lines-per-command dưới một thư mục durable (systemd StateDirectory). Ghi atomic
(tmp + os.replace) → không bao giờ để lại file nửa vời qua crash.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace

# ── local lifecycle states ───────────────────────────────────────────────────
L_RECEIVED = "RECEIVED"
L_ACCEPTED = "ACCEPTED"
L_RUNNING = "RUNNING"
L_OUTCOME_RECORDED = "OUTCOME_RECORDED"
L_REPORTED = "REPORTED"
L_ACKED = "ACKED"

# Đã có outcome cục bộ → tuyệt đối KHÔNG re-mutate, chỉ re-report.
_HAS_OUTCOME = frozenset({L_OUTCOME_RECORDED, L_REPORTED, L_ACKED})
# Chưa chắc mutation chạy → resume qua idempotency ledger (reconcile), KHÔNG blind retry.
_PRE_OUTCOME = frozenset({L_RECEIVED, L_ACCEPTED, L_RUNNING})


@dataclass(frozen=True)
class InboxEntry:
    command_id: str
    tenant_id: str
    payload: dict
    local_state: str = L_RECEIVED
    outcome: dict = field(default_factory=dict)

    @property
    def has_outcome(self) -> bool:
        return self.local_state in _HAS_OUTCOME

    @property
    def needs_reconcile(self) -> bool:
        """RUNNING chưa outcome = có thể mutation đã bắt đầu → reconcile, không blind retry."""
        return self.local_state == L_RUNNING

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "InboxEntry":
        return cls(**json.loads(raw))


class LocalInbox:
    """Durable local store keyed theo command_id. Một file JSON / command (atomic write)."""

    def __init__(self, root: str) -> None:
        self._root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, command_id: str) -> str:
        safe = command_id.replace("/", "_").replace("..", "_")
        return os.path.join(self._root, f"{safe}.json")

    def _write_atomic(self, entry: InboxEntry) -> None:
        path = self._path(entry.command_id)
        fd, tmp = tempfile.mkstemp(dir=self._root, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(entry.to_json())
                fh.flush()
                os.fsync(fh.fileno())          # bền qua power-loss/reboot
            os.replace(tmp, path)              # atomic rename
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def persist(self, command_id: str, *, tenant_id: str, payload: dict) -> InboxEntry:
        """Ghi RECEIVED. Idempotent: nếu đã có (redelivery) → trả entry hiện tại, KHÔNG reset."""
        existing = self.get(command_id)
        if existing is not None:
            return existing
        entry = InboxEntry(command_id=command_id, tenant_id=tenant_id, payload=payload,
                           local_state=L_RECEIVED)
        self._write_atomic(entry)
        return entry

    def set_state(self, command_id: str, state: str) -> InboxEntry:
        entry = self.get(command_id)
        if entry is None:
            raise KeyError(command_id)
        updated = replace(entry, local_state=state)
        self._write_atomic(updated)
        return updated

    def record_outcome(self, command_id: str, outcome: dict) -> InboxEntry:
        """Ghi outcome + chuyển OUTCOME_RECORDED trong MỘT lần ghi atomic (không mất outcome)."""
        entry = self.get(command_id)
        if entry is None:
            raise KeyError(command_id)
        updated = replace(entry, outcome=outcome, local_state=L_OUTCOME_RECORDED)
        self._write_atomic(updated)
        return updated

    def archive(self, command_id: str) -> None:
        """Chỉ gọi sau terminal ack từ Gateway (state=ACKED). Xoá file cục bộ."""
        path = self._path(command_id)
        if os.path.exists(path):
            os.unlink(path)

    # ── read / resume ─────────────────────────────────────────────────────────
    def get(self, command_id: str) -> InboxEntry | None:
        path = self._path(command_id)
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return InboxEntry.from_json(fh.read())

    def pending(self) -> list[InboxEntry]:
        """Mọi command chưa ACKED — dùng để resume sau agent restart/reboot."""
        out: list[InboxEntry] = []
        for name in sorted(os.listdir(self._root)):
            if not name.endswith(".json") or name.startswith(".tmp-"):
                continue
            with open(os.path.join(self._root, name)) as fh:
                entry = InboxEntry.from_json(fh.read())
            if entry.local_state != L_ACKED:
                out.append(entry)
        return out
