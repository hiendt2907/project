"""Omni Watchdog: Autonomous SRE diagnostic and self-healing agent."""

import asyncio
import logging
import re
import traceback
from typing import Any, Optional

from kubernetes_asyncio import client, config

from workers.settings import WorkerSettings

logger = logging.getLogger("omni.watchdog")

# Common SRE Error Patterns
ERR_AUTH_FAILED = re.compile(r"password authentication failed", re.IGNORECASE)
ERR_DB_NOT_FOUND = re.compile(r"database \".*\" does not exist", re.IGNORECASE)
ERR_MODULE_NOT_FOUND = re.compile(r"ModuleNotFoundError: No module named '(.*)'", re.IGNORECASE)
ERR_ATTR_ERROR = re.compile(
    r"AttributeError: '.*' object has no attribute 'vector_store'", re.IGNORECASE
)
ERR_PERMISSION_DENIED = re.compile(r"must be owner of table (.*)", re.IGNORECASE)

class OmniWatchdog:
    def __init__(self, ws: WorkerSettings):
        self.ws = ws
        self.k8s_v1: Optional[client.CoreV1Api] = None
        self.k8s_apps: Optional[client.AppsV1Api] = None

    async def start(self):
        logger.info("Initializing Omni Watchdog...")
        try:
            await self._init_k8s()
            await self._init_db()

            while True:
                try:
                    await self.check_and_heal()
                except Exception as e:
                    logger.error("Watchdog loop error: %s", e)
                await asyncio.sleep(60)
        finally:
            pass

    async def _init_k8s(self):
        try:
            config.load_incluster_config()
        except:
            await config.load_kube_config()
        self.k8s_v1 = client.CoreV1Api()
        self.k8s_apps = client.AppsV1Api()

    async def _init_db(self):
        # pg removed — Postgres/asyncpg dependency has been eliminated
        logger.info("pg removed")

    async def check_and_heal(self):
        logger.debug("Running SRE health check cycle...")
        
        # 1. Check Worker Pods
        pods = await self.k8s_v1.list_namespaced_pod(self.ws.k8s_default_namespace, label_selector="app=omni-worker")
        for pod in pods.items:
            status = pod.status.phase
            # Check for CrashLoopBackOff or Error
            container_statuses = pod.status.container_statuses or []
            for cs in container_statuses:
                if cs.state.waiting and cs.state.waiting.reason in ("CrashLoopBackOff", "Error"):
                    logger.warning("Pod %s is in %s. Analyzing logs...", pod.metadata.name, cs.state.waiting.reason)
                    await self._analyze_and_fix_pod(pod.metadata.name)

        # 2. Check Pgpool status
        await self._check_pgpool_nodes()

    async def _analyze_and_fix_pod(self, pod_name: str):
        try:
            logs = await self.k8s_v1.read_namespaced_pod_log(pod_name, self.ws.k8s_default_namespace, tail_lines=100)
            
            if ERR_AUTH_FAILED.search(logs) or ERR_DB_NOT_FOUND.search(logs):
                logger.info("Auth or DB name mismatch detected. Verifying secrets...")
                # In real scenario, we might re-apply the deployment or sync secrets
                # For now, we trigger a rollout just to pick up any secret changes
                await self._trigger_rollout("omni-worker")
                
            elif ERR_ATTR_ERROR.search(logs):
                logger.info("Stale code detected (vector_store attribute error). Triggering rebuild/rollout...")
                # We assume the builder job fixed the image, so a restart is enough
                await self._trigger_rollout("omni-worker")

            elif m := ERR_PERMISSION_DENIED.search(logs):
                table_name = m.group(1)
                logger.info("Permission issue on table %s. Fixing ownership...", table_name)
                await self._fix_db_ownership(table_name)

        except Exception as e:
            logger.error("Failed to analyze pod %s: %s", pod_name, e)

    async def _check_pgpool_nodes(self):
        # pg removed — no-op stub
        pass

    async def _fix_db_ownership(self, table_name: str):
        # pg removed — no-op stub
        logger.info("_fix_db_ownership stubbed (pg removed): table=%s", table_name)

    async def _trigger_rollout(self, deployment_name: str):
        logger.info("Triggering rollout restart for %s", deployment_name)
        import datetime
        now = datetime.datetime.now().isoformat()
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now
                        }
                    }
                }
            }
        }
        await self.k8s_apps.patch_namespaced_deployment(deployment_name, self.ws.k8s_default_namespace, body)

async def main():
    logging.basicConfig(level=logging.INFO)
    ws = WorkerSettings()
    watchdog = OmniWatchdog(ws)
    await watchdog.start()

if __name__ == "__main__":
    asyncio.run(main())
