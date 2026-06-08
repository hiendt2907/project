"""Seed curated vendor knowledge (basic→advanced) DIRECTLY into Redis `vendor_knowledge`.

No JSON artifact — the knowledge is defined in-code, embedded via Ollama, and upserted
into the same HNSW index the diagnosis brain reads. Each entry teaches an
investigate-the-WHY-then-trace-blast-radius pattern, grounded in vendor docs.

Run (lab):
  OMNI_OLLAMA_BASE_URL=http://localhost:11434 \
  PYTHONPATH=src .venv/bin/python -m training.kb_seed --redis-url redis://localhost:16379/0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import struct
import sys

import redis.asyncio as aioredis

from pkg.rag.ollama_embed import EMBED_DIM, embed_text

logger = logging.getLogger(__name__)

COLLECTION = "vendor_knowledge"


# (vendor, category, tier, title, situation, knowledge, score)
KB: list[tuple[str, str, str, str, str, str, int]] = [
    # ---- Kubernetes — pod memory / OOM (the exact gap we are fixing) ----
    ("Kubernetes", "memory", "basic",
     "Container OOMKilled is a cgroup-limit event, not a node-RAM event",
     "Pod shows lastState.terminated.reason=OOMKilled, restartCount rising.",
     "OOMKilled means the container exceeded its cgroup memory limit (resources.limits.memory); "
     "the kernel cgroup OOM-killer killed PID 1 of that container. The NODE may have plenty of free RAM. "
     "Investigate the container, not the host: read lastState.terminated.reason, restartCount, and compare "
     "limits.memory vs requests.memory. Do NOT run host `top`/`free` first — that answers the wrong scope. "
     "Only escalate to the node when you see a node MemoryPressure taint or kernel system-OOM in dmesg.", 88),
    ("Kubernetes", "memory", "intermediate",
     "Distinguish memory leak vs load spike vs under-provisioned limit",
     "Pod OOMs repeatedly; need the mechanism before proposing any fix.",
     "Pull container_memory_working_set_bytes over time. LEAK: working_set rises monotonically and never "
     "drops after GC/idle → fix is in the app (heap, cache, connection pool), not a restart. SPIKE: working_set "
     "tracks request rate/throughput → fix is autoscaling or rate control. UNDER-PROVISIONED: working_set is "
     "steady but just above limits.memory → raise the limit (and requests). A restart that ignores the mechanism "
     "only resets the clock to the next OOM. Always check `kubectl rollout history` for a recent limit/code change.", 90),
    ("Kubernetes", "memory", "advanced",
     "OOM blast radius: a crashing consumer degrades the whole pipeline",
     "A repeatedly-OOMing pod is part of a Kafka consumer-group / Service backend.",
     "Trace downstream impact, do not stop at 'the pod restarts'. A pod in CrashLoop leaves its Kafka "
     "consumer-group → triggers a group rebalance (~seconds of stop-the-world) → partitions reassigned → "
     "consumer lag grows → end-to-end MTTD/processing latency rises. If it was the last Ready replica behind a "
     "Service, endpoints drop to 0 and dependent callers get connection-refused/5xx. Quantify: lag delta, "
     "endpoints count, dependent error rate — that is the real incident, the OOM is just the trigger.", 92),
    ("Kubernetes", "scheduling", "intermediate",
     "CrashLoopBackOff: read the previous container logs, not the live ones",
     "Pod state CrashLoopBackOff with increasing back-off delay.",
     "The running container is already dead; `kubectl logs <pod>` shows the new attempt. Use "
     "`kubectl logs <pod> --previous` to see why the prior instance exited, and `kubectl describe pod` for the "
     "exit code + reason. Exit 137=SIGKILL (OOM/killed), 143=SIGTERM, 1/2=app error, 127=missing binary/cmd. "
     "Back-off is exponential up to 5m — the pod is not 'stuck', it is rate-limited restarts.", 80),
    ("Kubernetes", "scheduling", "basic",
     "ImagePullBackOff vs ErrImagePull",
     "Pod cannot start; events show ImagePullBackOff.",
     "ErrImagePull = the pull just failed; ImagePullBackOff = kubelet is backing off after repeated failures. "
     "Causes: wrong image tag, private registry without imagePullSecret, registry rate-limit/unreachable, or "
     "wrong architecture. Diagnose with `kubectl describe pod` (Events) — it names the exact registry error. "
     "This is read-only to diagnose; never 'fix' by deleting the deployment.", 72),
    ("Kubernetes", "probes", "intermediate",
     "Liveness probe restarts vs readiness probe traffic removal",
     "Pod is Running but flapping or receiving no traffic.",
     "A failing READINESS probe removes the pod from Service endpoints (no traffic) but does NOT restart it. "
     "A failing LIVENESS probe restarts the container. A too-aggressive liveness probe (short timeout/period) "
     "during a GC pause or cold start causes restart storms that look like crashes. Check probe thresholds in "
     "the spec before blaming the app; align initialDelaySeconds with real startup time.", 78),
    ("Kubernetes", "scheduling", "advanced",
     "Pending pods: it is Requests, not Limits, that gate scheduling",
     "Pods stuck Pending; cluster looks under-utilized.",
     "The scheduler places pods by resources.requests against node Allocatable, not by actual usage or limits. "
     "A cluster at 30% real CPU can still be unschedulable if requests are over-set. `kubectl describe pod` "
     "shows the FailedScheduling reason (Insufficient cpu/memory, taints, affinity). Fix by right-sizing requests "
     "or adding capacity — raising limits does nothing for Pending.", 82),

    # ---- Linux / systemd / kernel ----
    ("Linux", "storage", "basic",
     "Disk full: check inodes too, not just blocks",
     "Writes fail with ENOSPC but `df -h` shows free space.",
     "`df -h` shows block usage; you can exhaust INODES while blocks are free (millions of tiny files). "
     "Check `df -i`. Find offenders with `du -x --max-depth=1 /var | sort -h`. On a K8s node, /var/lib/kubelet "
     "and /var/lib/containerd are common culprits (logs, ephemeral volumes, images).", 70),
    ("Linux", "memory", "intermediate",
     "System OOM-killer vs container OOM",
     "Process killed; dmesg shows 'Out of memory: Killed process'.",
     "A SYSTEM OOM (kernel runs out of node RAM) is different from a cgroup/container OOM. `dmesg -T | grep -i "
     "oom` shows the victim and the oom_score. Node-level OOM means real RAM pressure → look at the biggest RSS "
     "consumers and at vm.overcommit settings. Container OOM stays inside one cgroup and the node is usually fine.", 80),
    ("Linux", "services", "basic",
     "systemd unit failed: read the journal for the unit",
     "`systemctl status` shows a service in failed state.",
     "Get the real reason with `journalctl -u <unit> --no-pager -n 100` and `systemctl show <unit> "
     "-p ExecMainStatus,Result`. Result=oom-kill, exit-code, timeout, or signal tells you the failure class. "
     "Restarting before reading the journal hides the root cause.", 68),
    ("Linux", "performance", "advanced",
     "High load average with low CPU = I/O wait or run-queue of D-state tasks",
     "Load average is high but CPU% looks idle.",
     "Load average counts runnable AND uninterruptible-sleep (D-state) tasks. High load + low CPU usually means "
     "I/O wait: confirm with `iostat -xz 1` (%util, await) and `top` (wa%). D-state processes are blocked on disk/NFS. "
     "The fix is at the storage layer, not adding CPU.", 84),

    # ---- PostgreSQL ----
    ("PostgreSQL", "database", "basic",
     "Too many connections: pool, do not raise max_connections blindly",
     "Errors: 'FATAL: sorry, too many clients already'.",
     "Each Postgres connection is a backend process with real memory cost. Raising max_connections trades RAM and "
     "can worsen contention. Diagnose with `SELECT count(*), state FROM pg_stat_activity GROUP BY state`. Lots of "
     "'idle in transaction' = an app leaking transactions; fix the app or add a pooler (PgBouncer) instead.", 76),
    ("PostgreSQL", "database", "intermediate",
     "Bloat and autovacuum: rising dead tuples slow scans",
     "Query latency creeps up; table much larger than its live rows.",
     "MVCC leaves dead tuples that autovacuum reclaims. If autovacuum can't keep up (long transactions hold the "
     "xmin horizon, or thresholds too high), bloat grows and scans slow. Check pg_stat_user_tables "
     "(n_dead_tup, last_autovacuum). Long-running/idle-in-transaction sessions block vacuum cluster-wide — kill or "
     "fix them; tune autovacuum scale factors per hot table.", 82),
    ("PostgreSQL", "database", "advanced",
     "WAL cannot archive/fsync → writes block → cascading API 500s",
     "Disk filling on the DB volume; commits hanging.",
     "If pg_wal fills (archiver failing, or replication slot retaining WAL) or the filesystem can't fsync, Postgres "
     "stops accepting writes. Blast radius: app commit handlers block → connection pool saturates → upstream API "
     "times out → load balancer returns 500/503. Check pg_stat_archiver, pg_replication_slots (inactive slots are a "
     "classic WAL-retention trap), and disk on the DB volume. Trace the full chain, not just 'DB slow'.", 90),

    # ---- MySQL / InnoDB / ProxySQL ----
    ("MySQL", "database", "intermediate",
     "Replication lag: Seconds_Behind_Master and the single-thread bottleneck",
     "Replica serving stale reads; SHOW REPLICA STATUS shows lag.",
     "Seconds_Behind_Master estimates lag but is misleading during stalls (NULL when SQL thread stopped). Classic "
     "cause: a long single-threaded apply (large transaction, missing PK on replicated table) or replica I/O "
     "saturation. Check Replica_SQL_Running, Last_SQL_Error, and enable parallel replication (replica_parallel_workers) "
     "for write-heavy workloads. Read traffic on a lagging replica = stale data blast radius.", 82),
    ("MySQL", "database", "advanced",
     "InnoDB: history list length and long transactions",
     "Writes slow; undo logs growing.",
     "A long-open transaction pins the undo history (History list length in SHOW ENGINE INNODB STATUS), bloating "
     "undo and slowing everything. Find it in information_schema.innodb_trx (trx_started). Also watch for row-lock "
     "waits (innodb_lock_waits). The fix is the offending transaction, not a server restart.", 84),
    ("ProxySQL", "database", "intermediate",
     "Backend marked SHUNNED: ProxySQL took a server out of rotation",
     "Some queries fail/timeout; ProxySQL stats show a shunned backend.",
     "ProxySQL shuns a backend after connection errors exceed mysql-shun_on_failures, then probes to bring it back. "
     "Check stats_mysql_connection_pool (status, ConnERR) and runtime_mysql_servers. A shunned writer = failed writes "
     "until failover. Diagnose the backend MySQL health first; ProxySQL is reflecting an upstream fault.", 80),

    # ---- Redis ----
    ("Redis", "cache", "basic",
     "maxmemory + eviction policy: OOM command errors vs key eviction",
     "Writes rejected with OOM, or keys disappearing.",
     "When used_memory hits maxmemory, behavior depends on maxmemory-policy: noeviction rejects writes with an OOM "
     "error; allkeys-lru/lfu evicts keys (data loss for a cache, disaster for a store). Check INFO memory "
     "(used_memory, maxmemory, evicted_keys). Decide: is this a cache (evict ok) or a store (must raise memory / shard)?", 74),
    ("Redis", "cache", "advanced",
     "Latency spikes: fork on RDB/AOF-rewrite and big-O commands",
     "Periodic Redis latency spikes correlate with persistence.",
     "Redis is single-threaded for command execution. Latency spikes come from: (1) fork() for RDB save / AOF "
     "rewrite on large datasets (copy-on-write page faults), (2) O(N) commands (KEYS, SMEMBERS on huge sets, large "
     "DEL). Use `redis-cli --latency`, SLOWLOG, and LATENCY DOCTOR. Replace KEYS with SCAN; unlink big keys with "
     "UNLINK (async). Schedule/limit persistence on big instances.", 86),
    ("Redis", "persistence", "intermediate",
     "AOF everysec vs RDB: what you actually lose on crash",
     "Need durability guarantees for Redis-as-source-of-truth.",
     "RDB is point-in-time snapshots (save rules) — a crash loses everything since the last snapshot. AOF logs every "
     "write; appendfsync everysec loses at most ~1s on crash, always is safest but slowest. Enable appendonly yes "
     "with aof-use-rdb-preamble for fast reload. For durability, AOF everysec on a persistent volume is the standard "
     "lab/prod baseline.", 80),

    # ---- Nginx / HTTP ----
    ("Nginx", "http", "basic",
     "499 is the client closing the connection, not a server fault",
     "Access logs full of status 499.",
     "499 is an Nginx-specific code: the CLIENT closed the connection before the upstream responded. It is "
     "informational, usually slow upstream or impatient clients/timeouts — NOT a 5xx server error. Don't page on "
     "499 as a server fault; correlate with upstream_response_time to see if the backend is slow.", 70),
    ("Nginx", "http", "intermediate",
     "502 vs 503 vs 504 point at different layers",
     "Gateway returning 5xx; need to localize the fault.",
     "502 Bad Gateway = upstream returned an invalid/closed response (backend crashed or refused). 503 Service "
     "Unavailable = no healthy upstream / overloaded / rate-limited. 504 Gateway Timeout = upstream too slow past "
     "proxy_read_timeout. Read upstream_addr + upstream_status in the access log to find which backend, then "
     "diagnose THAT backend. The LB is the messenger.", 80),

    # ---- Kafka ----
    ("Kafka", "messaging", "intermediate",
     "Consumer lag: producers outpacing consumers or a stuck partition",
     "Consumer-group lag rising on omni topics.",
     "Lag = log-end-offset − committed-offset per partition. Rising lag means consumers can't keep up or are stuck. "
     "Check per-partition lag (not just total) — one hot/stuck partition skews it. Causes: slow handler, frequent "
     "rebalances (consumer crashes/OOM), or under-partitioned topic. A crashing consumer triggers rebalances that "
     "make lag worse — stabilize the consumer first.", 82),
    ("Kafka", "messaging", "advanced",
     "Under-replicated partitions and ISR shrink",
     "Topic shows under-replicated partitions; durability at risk.",
     "ISR (in-sync replicas) shrinks when a broker falls behind (replica.lag.time.max.ms) or is down. "
     "Under-min.insync.replicas with acks=all blocks producers. Check broker health, disk, and network before "
     "touching topic config. Losing ISR is a durability/availability blast radius, not just a metric.", 84),

    # ---- DNS / CoreDNS ----
    ("CoreDNS", "network", "intermediate",
     "In-cluster DNS failures cascade into 'connection refused' everywhere",
     "Many services intermittently fail to reach each other by name.",
     "K8s service discovery is DNS via CoreDNS. If CoreDNS is OOMing, throttled, or its upstream is slow, name "
     "resolution times out and apps log connection errors that look like the target is down — but the target is "
     "healthy. Check CoreDNS pod health/restarts, query latency, and ndots/search-domain blowups. One DNS fault "
     "presents as N service faults — diagnose DNS before chasing each caller.", 86),
]


async def run_seed(redis_url: str, *, dry_run: bool = False) -> int:
    r = aioredis.from_url(redis_url, decode_responses=True)
    try:
        await r.ping()
    except Exception as e:
        logger.error("redis connect failed: %s", e)
        return 1

    written = 0
    for i, (vendor, category, tier, title, situation, knowledge, score) in enumerate(KB):
        entry_id = f"kb-seed-{i:03d}"
        text = f"{title}\n{situation}\n{knowledge}".strip()
        try:
            vector = await embed_text(text)
        except Exception as e:
            logger.error("embed failed id=%s err=%s", entry_id, e)
            return 2
        payload = {
            "title": title, "summary": title, "knowledge": knowledge, "situation": situation,
            "vendor": vendor, "category": category, "tier": tier, "score": score,
            "source": "kb_seed", "type": "vendor_kb", "text": text,
        }
        if dry_run:
            logger.info("[dry] %s %s/%s (%s) score=%d", entry_id, vendor, category, tier, score)
            written += 1
            continue
        await r.hset(
            f"doc:{COLLECTION}:{entry_id}",
            mapping={
                "embedding": struct.pack(f"{EMBED_DIM}f", *vector),
                "omni_payload": json.dumps(payload, ensure_ascii=False),
                "text_content": f"{title} {knowledge}"[:4000],
                "source": "kb_seed",
                "doc_type": "vendor_kb",
            },
        )
        written += 1
        logger.info("seeded %s %s/%s (%s) score=%d", entry_id, vendor, category, tier, score)

    logger.info("kb_seed complete: wrote=%d collection=%s", written, COLLECTION)
    await r.aclose()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Seed vendor knowledge into Redis vendor_knowledge")
    p.add_argument("--redis-url", default=os.environ.get("OMNI_REDIS_URL", "redis://localhost:16379/0"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(asyncio.run(run_seed(args.redis_url, dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
