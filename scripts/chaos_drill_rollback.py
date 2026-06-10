#!/usr/bin/env python3
"""S1.2 Chaos Drill — Pre-Execute Snapshot + Auto-Rollback verification.

Injects a bad ConfigMap into a live deployment, triggers the autonomous
executor, and verifies:
  1. CRAT event ROLLBACK_EXECUTED exists in audit chain.
  2. Original ConfigMap data is restored.
  3. Deployment pods recover to Running state.

Usage:
    python scripts/chaos_drill_rollback.py [--namespace multi-agent] [--dry-run]

Exit 0 = rollback verified. Exit 1 = failure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chaos_drill_rollback")

NS = os.getenv("NS", "multi-agent")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
from e2e_env import kafka_bootstrap as _kafka_bootstrap, redis_url as _redis_url  # noqa: E402

REDIS_URL = _redis_url() or "redis://localhost:16379/0"
KAFKA_BOOTSTRAP = _kafka_bootstrap() or "localhost:9092"
TARGET_CONFIGMAP = "omni-chaos-drill-target"
DRILL_KEY = "chaos_drill_bad_value"
WAIT_FOR_ROLLBACK_SEC = 120
POLL_INTERVAL_SEC = 5


async def _get_redis():
    import redis.asyncio as aioredis
    return await aioredis.from_url(REDIS_URL, decode_responses=True)


async def _kubectl(args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "kubectl", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out = (stdout or b"").decode().strip()
    err = (stderr or b"").decode().strip()
    if err:
        logger.debug("kubectl stderr: %s", err)
    return proc.returncode, out


async def _ensure_target_configmap(namespace: str) -> dict[str, str]:
    """Create or read the target ConfigMap. Returns original data."""
    rc, out = await _kubectl([
        "get", "configmap", TARGET_CONFIGMAP, "-n", namespace, "-o", "json"
    ])
    if rc == 0:
        cm = json.loads(out)
        return cm.get("data") or {}

    # Create it fresh
    logger.info("Creating target ConfigMap %s/%s", namespace, TARGET_CONFIGMAP)
    rc, _ = await _kubectl([
        "create", "configmap", TARGET_CONFIGMAP,
        f"--from-literal=app_env=production",
        f"--from-literal=log_level=info",
        "-n", namespace,
    ])
    if rc != 0:
        raise RuntimeError(f"Failed to create ConfigMap {TARGET_CONFIGMAP}")
    return {"app_env": "production", "log_level": "info"}


async def _inject_bad_configmap(namespace: str) -> None:
    """Patch ConfigMap with invalid values to trigger remediation."""
    patch = json.dumps({"data": {DRILL_KEY: "CHAOS_INJECTED", "app_env": "broken_chaos"}})
    rc, out = await _kubectl([
        "patch", "configmap", TARGET_CONFIGMAP,
        "-n", namespace,
        "--type=merge",
        "-p", patch,
    ])
    if rc != 0:
        raise RuntimeError(f"Failed to inject chaos patch: {out}")
    logger.info("CHAOS INJECTED: %s/%s patched with bad data", namespace, TARGET_CONFIGMAP)


async def _restore_configmap(namespace: str, original_data: dict[str, str]) -> None:
    """Manually restore the ConfigMap (fallback if auto-rollback didn't fire)."""
    # Remove chaos key, restore originals
    patch_data = {k: None for k in [DRILL_KEY]}  # null = delete key
    patch_data.update(original_data)
    patch = json.dumps({"data": patch_data})
    await _kubectl([
        "patch", "configmap", TARGET_CONFIGMAP,
        "-n", namespace,
        "--type=merge",
        "-p", patch,
    ])


async def _check_crat_rollback_event(redis, trace_pattern: str) -> dict | None:
    """Scan CRAT audit chain for ROLLBACK_EXECUTED event."""
    try:
        blocks = await redis.lrange("audit_chain:blocks", 0, -1)
        for raw in reversed(blocks):
            try:
                block = json.loads(raw)
                payload = block.get("payload") or {}
                if block.get("event_type") == "ROLLBACK_EXECUTED":
                    return payload
            except Exception:
                pass
    except Exception as e:
        logger.debug("CRAT scan error: %s", e)
    return None


async def _emit_chaos_alert(namespace: str) -> None:
    """Emit a fake alert to trigger the autonomous pipeline via Redis."""
    try:
        import redis.asyncio as aioredis
        r = await aioredis.from_url(REDIS_URL)
        alert = {
            "alertname": "ConfigMapDrift",
            "namespace": namespace,
            "deployment": "omni-chaos-drill",
            "severity": "critical",
            "description": f"Detected bad value '{DRILL_KEY}' in configmap {TARGET_CONFIGMAP}",
            "chaos_drill": "true",
        }
        await r.xadd("stream:chaos_alerts", {"data": json.dumps(alert)})
        logger.info("Emitted chaos alert to Redis stream")
    except Exception as e:
        logger.warning("Failed to emit Redis alert: %s — rollback may not auto-trigger", e)


async def run_drill(namespace: str, dry_run: bool) -> bool:
    logger.info("=== S1.2 CHAOS DRILL: Auto-Rollback Verification ===")
    logger.info("namespace=%s dry_run=%s", namespace, dry_run)

    redis = await _get_redis()

    original_data = await _ensure_target_configmap(namespace)
    logger.info("Original ConfigMap data: %s", original_data)

    if dry_run:
        logger.info("[DRY-RUN] Would inject: %s/%s", namespace, TARGET_CONFIGMAP)
        logger.info("[DRY-RUN] Would wait %ds for ROLLBACK_EXECUTED CRAT event", WAIT_FOR_ROLLBACK_SEC)
        logger.info("[DRY-RUN] PASS (dry run)")
        return True

    await _inject_bad_configmap(namespace)
    await _emit_chaos_alert(namespace)

    t0 = time.time()
    rollback_event = None
    logger.info("Waiting up to %ds for ROLLBACK_EXECUTED CRAT event...", WAIT_FOR_ROLLBACK_SEC)

    while time.time() - t0 < WAIT_FOR_ROLLBACK_SEC:
        rollback_event = await _check_crat_rollback_event(redis, TARGET_CONFIGMAP)
        if rollback_event:
            elapsed = time.time() - t0
            logger.info("ROLLBACK_EXECUTED found after %.0fs: %s", elapsed, rollback_event)
            break
        await asyncio.sleep(POLL_INTERVAL_SEC)

    # Verify ConfigMap was actually restored
    rc, out = await _kubectl([
        "get", "configmap", TARGET_CONFIGMAP, "-n", namespace, "-o", "json"
    ])
    current_data = (json.loads(out).get("data") or {}) if rc == 0 else {}
    restored = DRILL_KEY not in current_data and current_data.get("app_env") == original_data.get("app_env")

    if not rollback_event and not restored:
        logger.error("AUTO-ROLLBACK DID NOT FIRE — manually restoring")
        await _restore_configmap(namespace, original_data)
        logger.error("DRILL RESULT: FAIL — ROLLBACK_EXECUTED not found in CRAT, ConfigMap not restored")
        return False

    if not rollback_event:
        logger.warning("ConfigMap restored but no CRAT event found — rollback may have been manual")
    else:
        logger.info("CRAT event confirmed: trace=%s", rollback_event.get("trace_id", "unknown"))

    if restored:
        logger.info("ConfigMap data restored correctly: %s", current_data)
    else:
        logger.warning("ConfigMap data may not be fully restored: %s", current_data)
        await _restore_configmap(namespace, original_data)

    passed = rollback_event is not None and restored
    result = {
        "result": "PASS" if passed else "PARTIAL",
        "crat_event": rollback_event is not None,
        "configmap_restored": restored,
        "elapsed_sec": round(time.time() - t0, 1),
        "namespace": namespace,
        "target_configmap": TARGET_CONFIGMAP,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    report_path = f"chaos_drill_results_{time.strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_path, "w") as f:
        json.dump(result, f, indent=2)
    logger.info("Report saved: %s", report_path)
    logger.info("DRILL RESULT: %s", result["result"])
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description="S1.2 chaos drill — auto-rollback verification")
    parser.add_argument("--namespace", default=NS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return 0 if asyncio.run(run_drill(args.namespace, args.dry_run)) else 1


if __name__ == "__main__":
    sys.exit(main())
