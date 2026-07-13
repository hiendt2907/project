"""AOIP employee entrypoint — MỘT process, HAI vòng song song (Sprint NV-SRE IT-4).

    python -m aoip.agent.employee

Vòng 1 (telemetry): reuse NGUYÊN VẸN ``remote_agent.agent.run_agent()`` làm library —
collectors/emitter/discovery đã battle-tested, KHÔNG copy code. Employee chỉ tiêm
``extra_register_fields`` để register mang thêm ``aoip_bundle_sha256`` (drift IT-2
mở rộng: manifest publish cả 2 hash; agent legacy không báo aoip hash → không bị
đánh drifted oan).

Vòng 2 (daemon): ``aoip.agent.daemon.run_daemon()`` — durable command loop (ADR-001).
Runtime mode chọn qua ``AOIP_AGENT_MODE`` (mặc định ``observe_only``, fail-safe).

Shutdown/crash semantics (systemd):
- ``run_daemon`` đã own SIGTERM/SIGINT — employee KHÔNG đăng ký handler riêng.
  Daemon return sạch → cancel telemetry → exit 0.
- Vòng nào crash → exception propagate (exit non-zero) → systemd restart. KHÔNG
  nuốt lỗi để process "sống" mà mù một nửa.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from remote_agent.bundle_hash import compute_bundle_hash

_DEFAULT_INBOX = "/var/lib/aoip/inbox"
_DEFAULT_INTERVAL_S = 15


def aoip_self_bundle_hash() -> str:
    """Hash canonical của package ``aoip`` đang chạy — cùng thuật toán publisher
    (``scripts/publish_agent_release.py``) nên so sánh được với manifest."""
    import aoip

    return compute_bundle_hash(Path(aoip.__file__).resolve().parent)


def _install_dir() -> Path:
    """Thư mục chứa 2 package ship lên VM (mặc định /opt/omni-remote-agent) —
    suy từ vị trí package đang chạy để không phụ thuộc env."""
    env_dir = os.environ.get("OMNI_AGENT_INSTALL_DIR", "").strip()
    if env_dir:
        return Path(env_dir).resolve()
    import remote_agent

    return Path(remote_agent.__file__).resolve().parent.parent


def _build_default_daemon():
    """Dựng coroutine daemon production từ env ``OMNI_AGENT_*`` (run.env) +
    ``AOIP_AGENT_*``. Chỉ gọi khi caller không tiêm ``daemon`` (test inject không
    cần env)."""
    from aoip.agent.daemon import run_daemon
    from aoip.agent.omni_client import HTTPOmniClient
    from aoip.agent.runtime_config import MODE_OBSERVE_ONLY, build_agent_runtime
    from aoip.agent.updater import make_update_executor, make_update_reconciler
    from remote_agent.settings import AgentSettings

    settings = AgentSettings()
    mode = os.environ.get("AOIP_AGENT_MODE", MODE_OBSERVE_ONLY).strip().lower()
    executor, status = build_agent_runtime(mode=mode, agent_id=settings.agent_id)
    print(f"[bootstrap] {status.render()}")
    # IT-5: verb UPDATE_AGENT rẽ sang safe-update (được phép cả observe_only —
    # chỉ mutate install dir của chính agent); verb khác giữ executor theo mode.
    client = HTTPOmniClient(settings.gateway_url, api_key=settings.api_key)
    install = _install_dir()
    return run_daemon(
        agent_id=settings.agent_id,
        tenant=settings.tenant_id,
        gateway=settings.gateway_url,
        api_key=settings.api_key,
        inbox_root=os.environ.get("AOIP_AGENT_INBOX", _DEFAULT_INBOX),
        interval_s=int(os.environ.get("AOIP_AGENT_INTERVAL_S", str(_DEFAULT_INTERVAL_S))),
        executor=make_update_executor(executor, client=client, install_dir=install),
        reconciler=make_update_reconciler(install_dir=install),
        client=client,
    )


async def run_employee(*, telemetry=None, daemon=None) -> None:
    """Chạy 2 vòng tới khi MỘT vòng kết thúc: cancel vòng còn lại rồi propagate
    kết quả của vòng đã xong (return sạch → exit 0; crash → raise)."""
    if telemetry is None:
        from remote_agent.agent import run_agent

        telemetry = run_agent(
            extra_register_fields={"aoip_bundle_sha256": aoip_self_bundle_hash()})
    if daemon is None:
        daemon = _build_default_daemon()

    tasks = {asyncio.ensure_future(telemetry), asyncio.ensure_future(daemon)}
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()


def main() -> None:
    # IT-5 health-gate: chạy TRƯỚC mọi vòng. Update pending → commit (hash khớp)
    # hoặc rollback N-1; rollback cần restart để nạp lại code cũ (process này
    # đã import code mới) → exit non-zero, systemd Restart=always lo phần còn lại.
    from aoip.agent.updater import startup_gate

    gate = startup_gate(install_dir=_install_dir())
    if gate is not None:
        print(f"[updater] health-gate: {gate['update_status']} v{gate.get('version', '?')}")
        if gate.get("needs_restart"):
            raise SystemExit("updater rolled back — restarting to load previous bundle")
    asyncio.run(run_employee())


if __name__ == "__main__":
    main()
