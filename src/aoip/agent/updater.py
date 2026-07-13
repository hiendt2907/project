"""Safe self-update qua durable command channel (Sprint NV-SRE IT-5).

Đóng ACCEPT-GAP #12 của parity checklist IT-4: UPDATE_AGENT trên AOIP daemon với
health-gate + auto-rollback — thay cho ``remote_agent/updater.py`` (legacy: có
download+sha256+restart nhưng KHÔNG health-gate, KHÔNG N-1 bundle bền).

Luồng (update là durable command — tận dụng inbox/reconcile sẵn có, KHÔNG chế
thêm state machine):

  1. Omni enqueue ``/webhook/agent/rt/commands/enqueue`` payload
     ``{verb: UPDATE_AGENT, version, release_tar_sha256, bundle_sha256,
        aoip_bundle_sha256}``.
  2. Executor (``make_update_executor``) tải bundle TỪ CHÍNH GATEWAY của agent
     (kênh đã xác thực Bearer — không URL ngoài, không SSRF/host-whitelist),
     verify sha256 tarball vs payload, backup N-1 (`releases/previous.tar.gz`,
     thư mục BỀN — không /tmp), extract đè install dir, ghi marker
     ``pending.json`` rồi yêu cầu systemd restart. Process CHẾT giữa RUNNING —
     inbox durable giữ entry, KHÔNG mất outcome (chính là thiết kế).
  3. Boot mới: ``startup_gate()`` chạy TRƯỚC mọi vòng — health-gate: self-hash
     2 package vs hash kỳ vọng trong marker. Khớp → ``result.json``
     status=updated; lệch → restore N-1 + result rolled_back + restart lại.
  4. Bundle hỏng đến mức Python không boot nổi → gate không bao giờ chạy →
     ``scripts/aoip-agent-guard.sh`` (ExecStartPre, NGOÀI bundle được hash) đếm
     boot; quá ``_MAX_BOOT_ATTEMPTS`` với pending còn đó → restore N-1 + ghi
     result rolled_back bằng shell.
  5. Daemon resume: entry UPDATE_AGENT đang RUNNING → ``update_reconciler`` đọc
     result marker → report terminal đúng 1 lần (COMPLETED/updated hoặc
     FAILED/rolled_back). Marker bị dọn sau khi đọc; nếu mất marker, reconcile
     fallback bằng self-hash vs payload.

Bất biến an toàn:
- INV_NO_WRITE exception duy nhất: chỉ ghi trong install dir + releases dir.
- Không bao giờ report ``updated`` trước khi health-gate pass trên process MỚI.
- Verb khác UPDATE_AGENT crash giữa RUNNING → ESCALATED (không blind retry).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import tarfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

VERB_UPDATE_AGENT = "UPDATE_AGENT"

_DEFAULT_RELEASES_DIR = "/var/lib/aoip/releases"
_PENDING_MARKER = "pending.json"
_RESULT_MARKER = "result.json"
_BOOTCOUNT_FILE = "bootcount"
_PREVIOUS_BUNDLE = "previous.tar.gz"
# Package ship lên VM — phải khớp scripts/publish_agent_release.py và guard shell.
_BUNDLE_PACKAGES = ("remote_agent", "aoip")
_MAX_BOOT_ATTEMPTS = 3
_RESTART_DELAY_S = 2.0
_SERVICE_NAME = "aoip-agent.service"

STATUS_UPDATED = "updated"
STATUS_ROLLED_BACK = "rolled_back"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, path)


def _self_hashes(install_dir: Path) -> dict[str, str]:
    """Hash canonical 2 package đang nằm trên đĩa — cùng thuật toán publisher."""
    from remote_agent.bundle_hash import compute_bundle_hash

    return {
        "bundle_sha256": compute_bundle_hash(install_dir / "remote_agent"),
        "aoip_bundle_sha256": compute_bundle_hash(install_dir / "aoip"),
    }


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Chỉ chấp nhận member nằm dưới đúng 2 package top-level, không path traversal."""
    out: list[tarfile.TarInfo] = []
    for m in tar.getmembers():
        name = m.name.lstrip("./")
        if name.startswith("/") or ".." in name.split("/"):
            continue
        top = name.split("/", 1)[0]
        if top in _BUNDLE_PACKAGES and (m.isfile() or m.isdir()):
            out.append(m)
    return out


def _snapshot_current(install_dir: Path, dest: Path) -> None:
    """Backup N-1: tar 2 package hiện tại vào releases dir (bền qua reboot)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        for pkg in _BUNDLE_PACKAGES:
            src = install_dir / pkg
            if src.exists():
                tar.add(src, arcname=pkg)
    os.replace(tmp, dest)


def _extract_bundle(data: bytes, install_dir: Path) -> None:
    """Extract đè: xoá package cũ rồi bung package mới (không để file mồ côi
    làm lệch bundle hash)."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = _safe_members(tar)
        if not members:
            raise ValueError("bundle_empty_or_unsafe")
        for pkg in _BUNDLE_PACKAGES:
            shutil.rmtree(install_dir / pkg, ignore_errors=True)
        tar.extractall(install_dir, members=members)  # noqa: S202 — members đã lọc


def restore_previous(install_dir: Path, releases_dir: Path) -> bool:
    """Restore bundle N-1. Trả False nếu không có backup (không raise —
    caller quyết định escalate)."""
    backup = releases_dir / _PREVIOUS_BUNDLE
    if not backup.exists():
        return False
    with tarfile.open(backup, "r:gz") as tar:
        members = _safe_members(tar)
        for pkg in _BUNDLE_PACKAGES:
            shutil.rmtree(install_dir / pkg, ignore_errors=True)
        tar.extractall(install_dir, members=members)  # noqa: S202
    return True


async def _default_restart() -> None:
    """Yêu cầu systemd restart chính service này. Process hiện tại sẽ chết —
    entry inbox đang RUNNING, resume sau boot xử lý tiếp (by design)."""
    await asyncio.sleep(_RESTART_DELAY_S)
    proc = await asyncio.create_subprocess_exec("systemctl", "restart", _SERVICE_NAME)
    await proc.wait()


def apply_update(payload: dict, bundle: bytes, *, install_dir: Path,
                 releases_dir: Path) -> dict:
    """Các bước KHÔNG-restart của update: verify → backup N-1 → extract →
    pending marker. Trả dict lỗi (``update_status`` != ok) hoặc ``{"ok": True}``.

    Tách khỏi executor để test được không cần process death.
    """
    version = str(payload.get("version", "")).strip()
    expected_tar = str(payload.get("release_tar_sha256", "")).strip().lower()
    if not version or len(expected_tar) < 32:
        return {"update_status": "invalid_payload",
                "detail": "version and release_tar_sha256 (>=32 chars) required"}

    actual = _sha256_bytes(bundle)
    if actual != expected_tar:
        logger.error("[updater] CHECKSUM_FAIL expected=%s actual=%s",
                     expected_tar[:16], actual[:16])
        return {"update_status": "checksum_fail",
                "detail": "release tar sha256 mismatch — aborted before install"}

    releases_dir.mkdir(parents=True, exist_ok=True)
    try:
        _snapshot_current(install_dir, releases_dir / _PREVIOUS_BUNDLE)
    except Exception as exc:  # noqa: BLE001 — không có backup thì KHÔNG được extract
        return {"update_status": "backup_fail", "detail": str(exc)[:256]}

    try:
        _extract_bundle(bundle, install_dir)
    except Exception as exc:  # noqa: BLE001
        restored = restore_previous(install_dir, releases_dir)
        return {"update_status": "install_fail",
                "detail": f"{str(exc)[:200]} restored={restored}"}

    _write_json_atomic(releases_dir / _PENDING_MARKER, {
        "command_id": str(payload.get("command_id", "")),
        "version": version,
        "expected": {
            "bundle_sha256": str(payload.get("bundle_sha256", "")),
            "aoip_bundle_sha256": str(payload.get("aoip_bundle_sha256", "")),
        },
        "applied_at": int(time.time()),
    })
    # bootcount mới cho chu kỳ health-gate này (guard shell tăng dần mỗi boot)
    (releases_dir / _BOOTCOUNT_FILE).unlink(missing_ok=True)
    logger.info("[updater] bundle v%s staged, pending health-gate after restart", version)
    return {"ok": True, "version": version}


def make_update_executor(base_executor, *, client, install_dir: str | Path,
                         releases_dir: str | Path = _DEFAULT_RELEASES_DIR,
                         restart=None):
    """Verb router: UPDATE_AGENT → safe update; verb khác → ``base_executor``.

    Update được phép cả ở observe_only — nó chỉ mutate install dir của CHÍNH
    agent (INV_NO_WRITE exception có chủ đích, như legacy updater), không đụng
    hệ thống khách hàng.
    """
    install = Path(install_dir)
    releases = Path(releases_dir)
    restart_fn = restart or _default_restart

    async def _executor(payload: dict) -> tuple[str, dict]:
        if payload.get("verb") != VERB_UPDATE_AGENT:
            return await base_executor(payload)

        try:
            bundle = await client.download_release_bundle()
        except Exception as exc:  # noqa: BLE001 — download fail = outcome, đừng giết daemon
            return "FAILED", {"rc": 1, "update_status": "download_fail",
                              "detail": str(exc)[:256]}

        result = apply_update(payload, bundle, install_dir=install, releases_dir=releases)
        if not result.get("ok"):
            return "FAILED", {"rc": 1, **result}

        # Restart để chạy code mới; process này chết giữa chừng — outcome thật
        # do startup_gate + reconciler quyết định sau boot. Chờ vô hạn là chủ ý:
        # KHÔNG được report terminal trước khi health-gate pass.
        await restart_fn()
        await asyncio.Event().wait()
        raise AssertionError("unreachable — process expected to die on restart")

    return _executor


def startup_gate(*, install_dir: str | Path,
                 releases_dir: str | Path = _DEFAULT_RELEASES_DIR) -> dict | None:
    """Health-gate sau restart — gọi ĐẦU TIÊN trong employee.main().

    pending marker + self-hash khớp expected → result ``updated``; lệch →
    restore N-1 + result ``rolled_back`` (caller PHẢI restart lại để nạp code
    cũ — xem giá trị ``needs_restart``). Không có pending → None (boot thường).
    """
    install = Path(install_dir)
    releases = Path(releases_dir)
    pending = _read_json(releases / _PENDING_MARKER)
    if pending is None:
        (releases / _BOOTCOUNT_FILE).unlink(missing_ok=True)
        return None

    expected = pending.get("expected", {})
    actual = _self_hashes(install)
    matched = all(
        not expected.get(k) or expected[k] == actual[k]
        for k in ("bundle_sha256", "aoip_bundle_sha256")
    )
    if matched:
        result = {"update_status": STATUS_UPDATED, "version": pending.get("version", ""),
                  "command_id": pending.get("command_id", ""), "needs_restart": False}
        logger.info("[updater] health-gate PASS — committed v%s", pending.get("version"))
    else:
        restored = restore_previous(install, releases)
        result = {"update_status": STATUS_ROLLED_BACK, "version": pending.get("version", ""),
                  "command_id": pending.get("command_id", ""),
                  "detail": f"hash_mismatch actual={actual} restored={restored}",
                  "needs_restart": True}
        logger.error("[updater] health-gate FAIL — rolled back to N-1 (restored=%s)", restored)

    _write_json_atomic(releases / _RESULT_MARKER, result)
    (releases / _PENDING_MARKER).unlink(missing_ok=True)
    (releases / _BOOTCOUNT_FILE).unlink(missing_ok=True)
    return result


def make_update_reconciler(*, install_dir: str | Path,
                           releases_dir: str | Path = _DEFAULT_RELEASES_DIR):
    """Reconciler cho DeliveryLoop.resume(): entry RUNNING sau crash/restart.

    UPDATE_AGENT: đọc result marker (do startup_gate/guard ghi) → terminal đúng
    1 lần; marker được dọn sau khi đọc (outcome đã persist vào inbox trước khi
    report). Mất marker → fallback so self-hash với expected trong payload.
    Verb khác: ESCALATED — mutation dở dang không rõ outcome, KHÔNG blind retry.
    """
    install = Path(install_dir)
    releases = Path(releases_dir)

    async def _reconcile(entry) -> tuple[str, dict]:
        if entry.payload.get("verb") != VERB_UPDATE_AGENT:
            return "ESCALATED", {"rc": 1, "reason": "crash_during_execution_unknown_outcome",
                                 "verb": entry.payload.get("verb", "")}

        marker_path = releases / _RESULT_MARKER
        result = _read_json(marker_path)
        if result is not None and result.get("command_id") in ("", entry.command_id):
            marker_path.unlink(missing_ok=True)
        else:
            # Marker mất/của command khác → suy outcome từ trạng thái đĩa thật.
            actual = _self_hashes(install)
            expected_aoip = str(entry.payload.get("aoip_bundle_sha256", ""))
            updated = bool(expected_aoip) and expected_aoip == actual["aoip_bundle_sha256"]
            result = {"update_status": STATUS_UPDATED if updated else STATUS_ROLLED_BACK,
                      "version": str(entry.payload.get("version", "")),
                      "detail": "derived_from_disk_state"}

        outcome = {"rc": 0 if result["update_status"] == STATUS_UPDATED else 1,
                   "update_status": result["update_status"],
                   "version": result.get("version", ""),
                   "detail": result.get("detail", "")}
        state = "COMPLETED" if result["update_status"] == STATUS_UPDATED else "FAILED"
        return state, outcome

    return _reconcile
