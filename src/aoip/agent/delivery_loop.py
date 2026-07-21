"""Agent durable delivery loop — persist trước execute, resume sau restart, report terminal.

Vòng đời một command ở agent (khớp DoD Step 3):

  receive → persist local inbox (RECEIVED) → accept → validate → execute/reconcile
          → persist local outcome → report terminal → nhận Gateway terminal ack
          → archive local command

Bất biến an toàn (never blindly retry a mutating command after unknown outcome):
- Đã có outcome cục bộ (OUTCOME_RECORDED) → chỉ RE-REPORT, KHÔNG execute lại.
- RUNNING chưa outcome khi resume → gọi ``reconcile`` (không blind re-execute).
- Chỉ archive khi Gateway trả terminal acknowledgement.

Loop KHÔNG cầm transport mutation trực tiếp — nhận ``executor``/``reconciler`` callable để tầng
mutation (operations/recovery) tiêm vào. Điều này giữ crash-safety tách khỏi business logic
và test được độc lập.

Delivery ownership/fencing (Gateway twin: ``src/gateway/routes/agent_runtime.py``): mỗi command
polled mang ``delivery_attempt``/``fencing_token``/``record_version`` của lần claim đó. Loop lưu
NGUYÊN VẸN ba field này vào inbox cục bộ tại lần persist ĐẦU và echo lại y hệt trên mọi
accept/progress/report_terminal — Gateway reject (409) nếu không khớp record hiện tại (stale
sau redelivery).

Long-running execution safety: trong lúc ``self._executor(entry.payload)`` chạy (có thể lâu
hơn Gateway ``visibility_deadline`` 60s mặc định), một coordinator nền gọi
``client.heartbeat_visibility()`` mỗi ``heartbeat_interval_s`` để gia hạn visibility — KHÔNG
đổi attempt/token, chỉ tránh Gateway redeliver TRONG LÚC agent vẫn đang chạy bình thường. Nếu
client KHÔNG implement ``heartbeat_visibility`` (fake/legacy trong test), coordinator bị bỏ qua
im lặng (backward-compat, KHÔNG phải production default thiếu tính năng). Nếu heartbeat bị
Gateway từ chối (409 — ownership_lost thật, KHÔNG phải lỗi mạng), coordinator KHÔNG huỷ
mutation đang chạy (side effect có thể đã xảy ra); ``report_terminal`` cuối cùng dùng attempt cũ
sẽ bị Gateway 409 (``stale_delivery_attempt``) — ``_report_and_archive`` GIỮ outcome cục bộ
(KHÔNG archive, KHÔNG coi là fail) để lần resume sau tự re-report/reconcile, KHÔNG blind retry.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Protocol

from aoip.agent.inbox import (
    L_ACCEPTED,
    L_ACKED,
    L_REPORTED,
    L_RUNNING,
    InboxEntry,
    LocalInbox,
)
from aoip.agent.renewal import run_with_renewal

logger = logging.getLogger(__name__)
_HEARTBEAT_INTERVAL_S = 15.0  # < Gateway _VISIBILITY_S (60s), safety margin ~4x

# executor(payload) -> (terminal_state, outcome); reconciler(entry) -> (terminal_state, outcome)
Executor = Callable[[dict], Awaitable[tuple[str, dict]]]
Reconciler = Callable[[InboxEntry], Awaitable[tuple[str, dict]]]


class RuntimeDeliveryClient(Protocol):
    async def poll_runtime(self, agent_id: str) -> list[dict]: ...
    async def accept(self, agent_id: str, tenant_id: str, command_id: str, *,
                     delivery_attempt: int, fencing_token: str) -> None: ...
    async def progress(self, agent_id: str, tenant_id: str, command_id: str, phase: str, *,
                       delivery_attempt: int, fencing_token: str) -> None: ...
    async def report_terminal(self, agent_id: str, tenant_id: str, command_id: str,
                              state: str, outcome: dict, *,
                              delivery_attempt: int, fencing_token: str) -> dict: ...


class DeliveryLoop:
    def __init__(self, *, agent_id: str, client: RuntimeDeliveryClient, inbox: LocalInbox,
                 executor: Executor, reconciler: Reconciler | None = None,
                 heartbeat_interval_s: float = _HEARTBEAT_INTERVAL_S) -> None:
        self._agent_id = agent_id
        self._client = client
        self._inbox = inbox
        self._executor = executor
        self._reconciler = reconciler
        self._heartbeat_interval_s = heartbeat_interval_s

    async def resume(self) -> int:
        """Gọi khi agent khởi động. Xử lý mọi command chưa ACKED trong inbox cục bộ.

        Trả số command đã đưa về terminal-ack. KHÔNG blind re-execute mutating command.
        """
        handled = 0
        for entry in self._inbox.pending():
            if entry.has_outcome:
                await self._report_and_archive(entry, entry.outcome, _state_of(entry))
                handled += 1
            elif entry.needs_reconcile and self._reconciler is not None:
                state, outcome = await self._reconciler(entry)
                self._inbox.record_outcome(entry.command_id, outcome)
                await self._report_and_archive(self._inbox.get(entry.command_id), outcome, state)
                handled += 1
            # RECEIVED/ACCEPTED chưa RUNNING → an toàn để tick() xử lý lại từ đầu.
        return handled

    async def tick(self) -> int:
        """Một vòng: pull → persist → accept → execute → record → report → archive."""
        commands = await self._client.poll_runtime(self._agent_id)
        processed = 0
        for cmd in commands:
            cid = cmd["command_id"]
            tenant = cmd.get("tenant_id", "")
            existing = self._inbox.get(cid)
            if existing is not None and existing.has_outcome:
                # duplicate delivery của command đã chạy → re-report, ZERO re-mutation
                await self._report_and_archive(existing, existing.outcome, _state_of(existing))
                processed += 1
                continue

            # command_id injected into the payload dict (not just tracked as the
            # inbox/entry key) so executors that decode payload as a standalone
            # dict — operations.decode_recovery_command() in particular — can see
            # it. Without this, its _key_for() correlation-based idempotency key
            # requires command_id and silently falls back to a coarser
            # (tenant+scope+decision_goal+failure_mode+unit) key, which a later,
            # unrelated incident for the same unit can collide with (caught live
            # 2026-07-21: a fresh recovery command reconciled as "already done"
            # against a prior day's cached idempotency record, without re-checking
            # current state).
            entry = self._inbox.persist(
                cid, tenant_id=tenant, payload={**cmd.get("payload", {}), "command_id": cid},
                delivery_attempt=cmd.get("delivery_attempt", 0),
                fencing_token=cmd.get("fencing_token", ""),
                record_version=cmd.get("record_version", 0))
            self._inbox.set_state(cid, L_ACCEPTED)
            await self._client.accept(self._agent_id, tenant, cid,
                                      delivery_attempt=entry.delivery_attempt,
                                      fencing_token=entry.fencing_token)

            self._inbox.set_state(cid, L_RUNNING)
            await self._client.progress(self._agent_id, tenant, cid, "RUNNING",
                                        delivery_attempt=entry.delivery_attempt,
                                        fencing_token=entry.fencing_token)
            state, outcome = await self._run_executor_with_heartbeat(entry)

            self._inbox.record_outcome(cid, outcome)               # bền TRƯỚC khi report
            await self._report_and_archive(self._inbox.get(cid), outcome, state)
            processed += 1
        return processed

    async def _run_executor_with_heartbeat(self, entry: InboxEntry) -> tuple[str, dict]:
        """Chạy executor SONG SONG với heartbeat gia hạn Gateway visibility định kỳ.

        Ownership loss trong lúc heartbeat KHÔNG huỷ mutation đang chạy — chỉ log; hậu quả
        (report_terminal bị 409 stale) được xử lý riêng ở ``_report_and_archive``.
        """
        heartbeat = getattr(self._client, "heartbeat_visibility", None)
        if heartbeat is None:
            return await self._executor(entry.payload)  # client không hỗ trợ → bỏ qua im lặng

        async def _renew() -> bool:
            return await heartbeat(self._agent_id, entry.tenant_id, entry.command_id,
                                   delivery_attempt=entry.delivery_attempt,
                                   fencing_token=entry.fencing_token)

        renewal = await run_with_renewal(
            self._executor(entry.payload), renew_fn=_renew,
            interval_s=self._heartbeat_interval_s, label="delivery_visibility")
        return renewal.result

    async def _report_and_archive(self, entry: InboxEntry, outcome: dict, state: str) -> None:
        ack = await self._client.report_terminal(
            self._agent_id, entry.tenant_id, entry.command_id, state, outcome,
            delivery_attempt=entry.delivery_attempt, fencing_token=entry.fencing_token)
        if ack.get("conflict"):
            # Gateway từ chối (attempt stale — có thể do redelivery trong lúc mutation chạy
            # lâu). GIỮ outcome cục bộ nguyên vẹn (đã persist trước khi gọi hàm này), KHÔNG
            # archive, KHÔNG tự kết luận fail — lần resume/tick sau tự re-report/reconcile.
            logger.info("delivery_loop.terminal_report_conflict command_id=%s error=%s",
                       entry.command_id, ack.get("error"))
            return
        self._inbox.set_state(entry.command_id, L_REPORTED)
        if ack.get("acknowledged"):
            self._inbox.set_state(entry.command_id, L_ACKED)
            self._inbox.archive(entry.command_id)


def _state_of(entry: InboxEntry) -> str:
    """Terminal state suy từ outcome cục bộ (mặc định COMPLETED nếu rc==0)."""
    oc = entry.outcome or {}
    if oc.get("escalate"):
        return "ESCALATED"
    if oc.get("rc", 0) != 0 or oc.get("failed"):
        return "FAILED"
    return "COMPLETED"
