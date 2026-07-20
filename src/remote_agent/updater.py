"""Self-update handler for Omni remote agent.

INVARIANT INV_NO_WRITE exception: UPDATE_AGENT is the single write operation allowed.
All writes are confined to /tmp/ and OMNI_AGENT_INSTALL_DIR only.
Never touches customer data or application files.

Security invariants:
  INV_HTTPS_ONLY       — download_url must be https://
  INV_HOST_WHITELIST   — download host must be in OMNI_AGENT_UPDATE_ALLOWED_HOSTS
  INV_CHECKSUM_MANDATORY — sha256_checksum must be present and match
  INV_BACKUP_FIRST     — backup current binary before replacing
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT_S = 120
_RESTART_TIMEOUT_S = 30
_ALLOWED_HOSTS_ENV = "OMNI_AGENT_UPDATE_ALLOWED_HOSTS"
_INSTALL_DIR_ENV = "OMNI_AGENT_INSTALL_DIR"
_SYSTEMD_SERVICE = "omni-agent"


def _get_allowed_hosts() -> frozenset[str]:
    """Parse comma-separated domain whitelist from env. Empty = no updates allowed."""
    raw = os.environ.get(_ALLOWED_HOSTS_ENV, "").strip()
    if not raw:
        return frozenset()
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _validate_url(url: str, allowed_hosts: frozenset[str]) -> tuple[bool, str]:
    if not allowed_hosts:
        return False, f"{_ALLOWED_HOSTS_ENV} is not configured — updates disabled"
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False, f"scheme_not_allowed: {parsed.scheme!r} — only https"
        host = parsed.netloc.lower().split(":")[0]
        if not any(host == h or host.endswith("." + h) for h in allowed_hosts):
            return False, f"host_not_whitelisted: {host!r}"
    except Exception as exc:
        return False, f"url_parse_error: {exc}"
    return True, ""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_install_dir() -> Path:
    env_dir = os.environ.get(_INSTALL_DIR_ENV, "").strip()
    if env_dir:
        return Path(env_dir).resolve()
    return Path(sys.argv[0]).resolve().parent


async def _download(url: str, dest: Path, api_key: str = "") -> tuple[bool, str]:
    """Download the release bundle. The gateway's /webhook/agent/release/bundle
    route sits behind _require_api_key like every other agent route — without
    this header the download 401s whenever the cluster has any key configured
    (i.e. every non-lab deployment), even though the enqueue/poll/enroll calls
    all authenticate correctly."""
    try:
        import httpx
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=_DOWNLOAD_TIMEOUT_S,
        ) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return False, f"http_{resp.status_code}"
            dest.write_bytes(resp.content)
            return True, ""
    except Exception as exc:
        return False, str(exc)[:256]


def _extract_if_archive(src: Path, install_dir: Path) -> tuple[bool, str]:
    """If src is .tar.gz, extract into install_dir. Otherwise move as-is."""
    if src.suffix in (".gz", ".tgz") or src.name.endswith(".tar.gz"):
        try:
            with tarfile.open(src, "r:gz") as tar:
                members = [m for m in tar.getmembers() if not m.name.startswith("/")]
                tar.extractall(install_dir, members=members, filter="data")  # noqa: S202
            return True, ""
        except Exception as exc:
            return False, f"extract_error: {exc}"
    # Single file — move to install_dir using source filename
    dest = install_dir / src.name
    try:
        shutil.move(str(src), str(dest))
        dest.chmod(0o755)
        return True, ""
    except Exception as exc:
        return False, f"move_error: {exc}"


async def _restart_service() -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "restart", _SYSTEMD_SERVICE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err_bytes = await asyncio.wait_for(proc.communicate(), timeout=_RESTART_TIMEOUT_S)
        if proc.returncode != 0:
            return False, err_bytes.decode(errors="replace")[:256]
        return True, ""
    except asyncio.TimeoutError:
        return False, "systemctl restart timed out"
    except Exception as exc:
        return False, str(exc)[:256]


async def handle_update_command(
    cmd_id: str,
    version: str,
    download_url: str,
    sha256_checksum: str,
    current_version: str = "unknown",
    api_key: str = "",
) -> dict:
    """Execute self-update. Returns result dict compatible with command result schema.

    Steps: validate URL → download → verify checksum → backup → extract/replace → restart.
    On any failure after backup: restores backup before returning error.
    """
    t0 = time.monotonic()

    def _result(status: str, detail: str = "") -> dict:
        msg = f"UPDATE_AGENT status={status} from={current_version} to={version}"
        if detail:
            msg += f" detail={detail}"
        return {
            "cmd_id": cmd_id,
            "blocked": False,
            "stdout": msg,
            "stderr": detail if status != "success" else "",
            "rc": 0 if status == "success" else 1,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "update_status": status,
            "update_version": version,
        }

    # Step 1 — validate URL
    ok, reason = _validate_url(download_url, _get_allowed_hosts())
    if not ok:
        logger.error("[updater] URL_BLOCKED cmd_id=%s reason=%s", cmd_id, reason)
        return _result("url_blocked", reason)

    # Step 2 — checksum must be present before we even download
    if not sha256_checksum or len(sha256_checksum) < 32:
        return _result("checksum_missing", "sha256_checksum is required (min 32 chars)")

    install_dir = _get_install_dir()
    suffix = Path(download_url).suffix or ""
    if ".tar" in download_url:
        suffix = ".tar.gz"
    tmp_new = Path(f"/tmp/omni-agent-new-{version}-{cmd_id[:8]}{suffix}")
    tmp_backup = Path(f"/tmp/omni-agent-backup-{current_version}-{cmd_id[:8]}.tar.gz")

    # Step 3 — download
    logger.info("[updater] downloading version=%s url=%s", version, download_url)
    ok, err = await _download(download_url, tmp_new, api_key=api_key)
    if not ok:
        tmp_new.unlink(missing_ok=True)
        logger.error("[updater] DOWNLOAD_FAIL cmd_id=%s err=%s", cmd_id, err)
        return _result("download_fail", err)

    # Step 4 — verify SHA-256 (mandatory, abort if mismatch)
    actual = _sha256_file(tmp_new)
    if actual.lower() != sha256_checksum.lower():
        tmp_new.unlink(missing_ok=True)
        logger.error(
            "[updater] CHECKSUM_FAIL cmd_id=%s expected=%s actual=%s",
            cmd_id, sha256_checksum[:16], actual[:16],
        )
        return _result("checksum_fail", "sha256 mismatch — download aborted")

    logger.info("[updater] checksum OK, installing to %s", install_dir)

    # Step 5 — backup current installation
    try:
        if install_dir.exists():
            with tarfile.open(tmp_backup, "w:gz") as tar:
                tar.add(install_dir, arcname="agent")
            logger.info("[updater] backup saved to %s", tmp_backup)
    except Exception as exc:
        logger.warning("[updater] backup_failed (non-fatal): %s", exc)

    # Step 6 — extract / replace
    ok, err = _extract_if_archive(tmp_new, install_dir)
    if not ok:
        logger.error("[updater] INSTALL_FAIL cmd_id=%s err=%s", cmd_id, err)
        if tmp_backup.exists():
            try:
                with tarfile.open(tmp_backup, "r:gz") as tar:
                    tar.extractall(install_dir.parent, filter="tar")  # noqa: S202 — backup tự tạo, giữ symlink
                logger.info("[updater] backup restored after install failure")
            except Exception as rb_exc:
                logger.error("[updater] rollback_failed: %s", rb_exc)
        return _result("install_fail", err)

    # Step 7 — restart service
    logger.info("[updater] restarting %s ...", _SYSTEMD_SERVICE)
    ok, err = await _restart_service()
    if not ok:
        logger.error("[updater] RESTART_FAIL cmd_id=%s err=%s", cmd_id, err)
        # Restore backup + retry restart with old version
        if tmp_backup.exists():
            try:
                with tarfile.open(tmp_backup, "r:gz") as tar:
                    tar.extractall(install_dir.parent, filter="tar")  # noqa: S202 — backup tự tạo, giữ symlink
                await _restart_service()
                logger.info("[updater] rollback complete, old version restored")
            except Exception as rb_exc:
                logger.error("[updater] rollback_failed: %s", rb_exc)
        return _result("restart_fail", err)

    logger.info("[updater] UPDATE_SUCCESS version=%s", version)
    return _result("success")
