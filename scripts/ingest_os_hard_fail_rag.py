"""Ingest 1000 Q/A pairs for os_hard_fail_diagnostic collection.

250 scenarios × 4 pairs (entry + mid1 + mid2 + terminal) = 1000 points.
Embed once per text, upsert to Redis HNSW collection.

Run:
  PYTHONPATH=src .venv/bin/python scripts/ingest_os_hard_fail_rag.py \\
      --redis-url redis://localhost:16379/0
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import redis.asyncio as aioredis

from llm.factory import build_llm_client
from rag.redis_vector_store import (
    COLLECTION_OS_HARD_FAIL_DIAGNOSTIC,
    EMBED_DIM,
    PointStruct,
    RedisVectorStore,
    PostgresRAGSettings,
)
from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)

BATCH_SIZE = 32


# ---------------------------------------------------------------------------
# Embed helpers (reused from sop_ingest.py)
# ---------------------------------------------------------------------------

def _vecs_from_embed_response(resp: dict) -> list[list[float]]:
    if "embeddings" in resp and resp["embeddings"]:
        return [list(e) for e in resp["embeddings"]]
    if "embedding" in resp:
        return [list(resp["embedding"])]
    raise ValueError("embed response missing embedding(s)")


def _pad_vec(v: list[float]) -> list[float]:
    if len(v) == EMBED_DIM:
        return v
    if len(v) > EMBED_DIM:
        return v[:EMBED_DIM]
    return v + [0.0] * (EMBED_DIM - len(v))


def _point_id(text: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, text))


# ---------------------------------------------------------------------------
# Scenario data — 250 scenarios × 4 pairs = 1000 total
# ---------------------------------------------------------------------------

def _q(alertname: str, sev: str, ns: str, src: str, steps: list[tuple[str, str]]) -> str:
    header = f"alert={alertname} severity={sev} ns={ns} source={src}"
    lines = [header]
    for i, (probe, result) in enumerate(steps, start=1):
        lines.append(f"step{i}: probe={probe} result={result}")
    return "\n".join(lines)[:400]


def _make_scenario(
    domain: str,
    alertname: str,
    sev: str,
    ns: str,
    src: str,
    probe1: str,
    probe2: str,
    probe3: str,
    interpretation: str,
    root_cause: str,
    fix: str,
    confidence: float = 0.90,
) -> list[dict]:
    """Generate 4 pairs for one scenario."""
    q_entry = _q(alertname, sev, ns, src, [])
    q_mid1 = _q(alertname, sev, ns, src, [(probe1, "PASSED-no-anomaly")])
    q_mid2 = _q(alertname, sev, ns, src, [(probe1, "PASSED-no-anomaly"), (probe2, "PASSED-no-anomaly")])
    q_terminal = _q(alertname, sev, ns, src, [(probe1, "PASSED-no-anomaly"), (probe2, "FAILED-or-anomaly")])

    return [
        {"text": q_entry, "pair_type": "entry", "domain": domain, "next_check": probe1,
         "interpretation": interpretation, "approval_required": False},
        {"text": q_mid1, "pair_type": "mid", "domain": domain, "next_check": probe2,
         "interpretation": f"step1 clear; escalate to {probe2}", "approval_required": False},
        {"text": q_mid2, "pair_type": "mid", "domain": domain, "next_check": probe3,
         "interpretation": f"step2 clear; escalate to {probe3}", "approval_required": False},
        {"text": q_terminal, "pair_type": "terminal", "domain": domain, "next_check": "",
         "interpretation": interpretation, "root_cause": root_cause, "fix": fix,
         "confidence": confidence, "approval_required": False},
    ]


def _s(domain, alertname, sev, ns, src, p1, p2, p3, interp, rc, fix, conf=0.90):
    return _make_scenario(domain, alertname, sev, ns, src, p1, p2, p3, interp, rc, fix, conf)


# ---------------------------------------------------------------------------
# D0 — SystemD (40 scenarios)
# ---------------------------------------------------------------------------
D0 = [
    _s("D0_systemd","SystemdCriticalUnitFailed","critical","infra","alertmanager","systemd_units","disk_usage","swap_usage","systemd alert is genuine","nginx.service crashed OOM","restart nginx, raise memory limit"),
    _s("D0_systemd","ServiceRestartLoop","warning","infra","alertmanager","systemd_units","cron_jobs","disk_usage","service cycling","startup script fails missing dep","fix dependency, update unit file"),
    _s("D0_systemd","JournaldFull","critical","infra","prometheus","systemd_units","disk_usage","lvm_volumes","journal disk exhausted","journal partition full","vacuumctl --rotate, extend LV"),
    _s("D0_systemd","SystemdTimerMissed","warning","jobs","alertmanager","systemd_units","cron_jobs","disk_usage","timer did not fire","timer unit disabled after upgrade","systemctl enable timer, daemon-reload"),
    _s("D0_systemd","ServiceSocketLeak","critical","infra","alertmanager","systemd_units","tcp_connections","network_interfaces","socket accumulation","missing CloseOnExec","patch socket leak, restart service"),
    _s("D0_systemd","KernelOopsOnService","critical","infra","alertmanager","kernel_errors","systemd_units","disk_usage","kernel oops crashed service","kernel regression","apply kernel patch, pin version"),
    _s("D0_systemd","DBusSocketTimeout","warning","infra","prometheus","systemd_units","network_interfaces","dns_resolution","dbus unresponsive","socket timeout under load","increase dbus timeout, restart"),
    _s("D0_systemd","SystemdActivationFailed","critical","infra","alertmanager","systemd_units","disk_usage","network_interfaces","activation race","tmpfiles.d path missing","create tmpfiles path, daemon-reload"),
    _s("D0_systemd","CgroupMemoryLimit","critical","prod","prometheus","systemd_units","swap_usage","oom_events","cgroup OOM","service mem leak","increase cgroup limit, fix leak"),
    _s("D0_systemd","SystemdUnitMasked","warning","infra","alertmanager","systemd_units","cron_jobs","disk_usage","unit masked","unit masked after deploy","unmask, daemon-reload, start"),
    _s("D0_systemd","ServiceFileLockHang","critical","infra","alertmanager","systemd_units","disk_usage","tcp_connections","lock file stale","prior crash left lock","rm stale lock, restart"),
    _s("D0_systemd","SystemdScopeLimit","warning","infra","prometheus","systemd_units","tcp_connections","network_interfaces","tasks limit","TasksMax too low","increase TasksMax in unit drop-in"),
    _s("D0_systemd","BootUnitTimeout","critical","infra","alertmanager","systemd_units","disk_usage","network_interfaces","boot timeout","network wait timeout","set network-online.target timeout"),
    _s("D0_systemd","SystemdDropInMissing","critical","infra","alertmanager","systemd_units","disk_usage","lvm_volumes","drop-in missing","override removed by pkg update","restore drop-in, daemon-reload"),
    _s("D0_systemd","ServiceOOMKilled","critical","prod","prometheus","systemd_units","oom_events","swap_usage","OOM kill confirmed","mem spike beyond limit","raise limit, fix leak"),
    _s("D0_systemd","SystemdPrivateTmpFull","warning","infra","alertmanager","systemd_units","disk_usage","lvm_volumes","private tmp full","large tmp artifact","clean tmp, increase quota"),
    _s("D0_systemd","SystemctlDaemonReloadFailed","critical","infra","alertmanager","systemd_units","disk_usage","network_interfaces","reload failure","syntax error in unit file","fix unit syntax, daemon-reload"),
    _s("D0_systemd","ServiceCapabilityDenied","critical","infra","alertmanager","systemd_units","kernel_errors","network_interfaces","capability denied","CAP missing after hardening","add capability to unit"),
    _s("D0_systemd","SystemdWantsFailed","warning","infra","alertmanager","systemd_units","cron_jobs","disk_usage","wants dep failed","dependency unit crashed","fix dep, restart chain"),
    _s("D0_systemd","SystemdAfterDep","warning","infra","alertmanager","systemd_units","network_interfaces","dns_resolution","After= dep slow","network dep slow to start","increase timeout, parallelize"),
    _s("D0_systemd","SystemdMemoryPressure","critical","prod","prometheus","systemd_units","swap_usage","oom_events","memory pressure","swappiness too low, thrashing","tune vm.swappiness, add swap"),
    _s("D0_systemd","SystemdCPUAccounting","warning","infra","prometheus","systemd_units","kernel_errors","cron_jobs","CPU quota exceeded","tight CPUQuota","raise CPUQuota in unit"),
    _s("D0_systemd","SystemdSeccompDenied","critical","infra","alertmanager","systemd_units","kernel_errors","network_interfaces","seccomp violation","syscall not in whitelist","add syscall to seccomp profile"),
    _s("D0_systemd","SystemdIPAddressAllow","warning","infra","alertmanager","systemd_units","network_interfaces","dns_resolution","IP access denied","IPAddressDeny too strict","update IPAddressAllow"),
    _s("D0_systemd","SystemdReadOnlyPath","critical","infra","alertmanager","systemd_units","disk_usage","lvm_volumes","write to ReadOnlyPath","app writes to protected path","move write path outside RO bind"),
    _s("D0_systemd","SystemdEnvironmentMissing","critical","infra","alertmanager","systemd_units","disk_usage","network_interfaces","env var missing","env file deleted","restore env file, restart"),
    _s("D0_systemd","SystemdSyslogOverflow","warning","infra","prometheus","systemd_units","disk_usage","lvm_volumes","syslog overrun","log flood from app","add rate limiting to journald"),
    _s("D0_systemd","SystemdUnitCycle","critical","infra","alertmanager","systemd_units","cron_jobs","disk_usage","unit dep cycle","circular dependency after refactor","break cycle in unit Wants="),
    _s("D0_systemd","SystemdSliceOOM","critical","prod","prometheus","systemd_units","oom_events","swap_usage","slice OOM","shared slice mem exceeded","increase MemoryLimit on slice"),
    _s("D0_systemd","SystemdExecPathMissing","critical","infra","alertmanager","systemd_units","disk_usage","lvm_volumes","exec path missing","binary deleted","reinstall package, verify ExecStart"),
    _s("D0_systemd","SystemdCGroupV2Migration","warning","infra","alertmanager","systemd_units","kernel_errors","cron_jobs","cgroup v2 migration","controllers not enabled","enable cgroup v2 in kernel cmdline"),
    _s("D0_systemd","SystemdNotifyTimeout","critical","infra","alertmanager","systemd_units","network_interfaces","disk_usage","sd_notify timeout","app not sending READY=1","fix sd_notify or remove Type=notify"),
    _s("D0_systemd","SystemdUserService","warning","infra","alertmanager","systemd_units","cron_jobs","disk_usage","user service not running","linger not enabled","loginctl enable-linger"),
    _s("D0_systemd","SystemdTransientUnit","warning","infra","alertmanager","systemd_units","disk_usage","network_interfaces","transient unit expired","systemd-run scope ended","reschedule transient unit"),
    _s("D0_systemd","SystemdBusNameTaken","critical","infra","alertmanager","systemd_units","cron_jobs","disk_usage","dbus name collision","stale process holds dbus name","kill stale proc, restart"),
    _s("D0_systemd","SystemdAssertionFailed","critical","infra","alertmanager","systemd_units","disk_usage","lvm_volumes","assertion failed","ConditionPathExists= missing","create path or remove assertion"),
    _s("D0_systemd","SystemdRateLimited","warning","infra","alertmanager","systemd_units","disk_usage","cron_jobs","start rate limit","StartLimitIntervalSec too tight","increase StartLimitBurst"),
    _s("D0_systemd","SystemdRebootRequired","warning","infra","prometheus","systemd_units","kernel_errors","disk_usage","reboot required","kernel update pending","schedule maintenance reboot"),
    _s("D0_systemd","SystemdFreezerTimeout","critical","prod","alertmanager","systemd_units","oom_events","swap_usage","freezer timeout","cgroup freeze timeout","disable freezer or increase timeout"),
    _s("D0_systemd","SystemdSocketBacklog","critical","prod","alertmanager","systemd_units","tcp_connections","network_interfaces","socket backlog full","Backlog= too small","increase socket backlog in unit"),
]

# ---------------------------------------------------------------------------
# D1 — Process (30 scenarios)
# ---------------------------------------------------------------------------
D1 = [
    _s("D1_process","OOMKillPodWorker","critical","prod","prometheus","oom_events","swap_usage","systemd_units","OOM kill genuine","java heap blowout","increase container memory limit"),
    _s("D1_process","ZombieProcessAccumulation","warning","infra","prometheus","zombie_processes","cron_jobs","systemd_units","zombie accumulation","parent not reaping","fix wait() in parent or use subreaper"),
    _s("D1_process","CronJobSilentFail","warning","jobs","alertmanager","cron_jobs","disk_usage","systemd_units","cron failed genuine","backup script missing dep","fix dep, test cron manually"),
    _s("D1_process","ForkBombDetected","critical","prod","alertmanager","zombie_processes","oom_events","systemd_units","fork bomb","runaway fork loop","kill process tree, set prlimit"),
    _s("D1_process","ProcTableFull","critical","prod","prometheus","zombie_processes","oom_events","swap_usage","pid exhausted","zombie accumulation + fork rate","reap zombies, lower pid_max guard"),
    _s("D1_process","OOMKillDatabaseProcess","critical","db","prometheus","oom_events","swap_usage","disk_usage","DB OOM genuine","buffer pool overallocated","reduce innodb_buffer_pool_size"),
    _s("D1_process","CronJobTimeout","warning","jobs","alertmanager","cron_jobs","disk_usage","network_interfaces","cron timeout","network dep slow","add timeout, fix dep"),
    _s("D1_process","ZombieParentCrash","critical","prod","alertmanager","zombie_processes","systemd_units","disk_usage","parent crashed leaving zombies","SIGSEGV in parent","fix parent crash, use supervisor"),
    _s("D1_process","ProcessRLimitNofile","critical","prod","prometheus","zombie_processes","tcp_connections","network_interfaces","fd limit exhausted","nofile rlimit too low","raise nofile in limits.conf"),
    _s("D1_process","CronJobStorageFull","critical","jobs","alertmanager","cron_jobs","disk_usage","lvm_volumes","cron aborted disk full","backup left large temp files","clean temp, extend volume"),
    _s("D1_process","MemLeakSlowBurn","warning","prod","prometheus","oom_events","swap_usage","systemd_units","slow mem leak","GC not returning to OS","tune GC, schedule restart"),
    _s("D1_process","ProcessSignalIgnored","warning","infra","alertmanager","zombie_processes","systemd_units","disk_usage","signal handler bug","SIGTERM ignored","fix signal handler, use SIGKILL fallback"),
    _s("D1_process","CronJobLockStale","warning","jobs","alertmanager","cron_jobs","disk_usage","systemd_units","cron lock file stale","prior run crashed","rm stale lock, restart cron"),
    _s("D1_process","OOMKillJVMHeap","critical","prod","prometheus","oom_events","swap_usage","disk_usage","JVM OOM genuine","heap < live set","increase -Xmx, tune GC"),
    _s("D1_process","ZombieDockerContainer","warning","infra","prometheus","zombie_processes","docker_daemon","containerd_state","zombie from container","PID 1 not reaping","use tini as PID 1"),
    _s("D1_process","CronJobNetworkFail","warning","jobs","alertmanager","cron_jobs","network_interfaces","dns_resolution","cron network dep fail","DNS resolution timeout","fix DNS, add retry to script"),
    _s("D1_process","ProcessRLimitStack","critical","prod","prometheus","oom_events","zombie_processes","systemd_units","stack overflow","unlimited recursion","fix recursion, set ulimit -s"),
    _s("D1_process","CronJobPermissionDenied","warning","jobs","alertmanager","cron_jobs","disk_usage","systemd_units","cron permission denied","file ownership changed","fix ownership, test as cron user"),
    _s("D1_process","OOMKillNodeExporter","warning","infra","prometheus","oom_events","swap_usage","systemd_units","node exporter OOM","large /proc scrape","restrict scrape or increase limit"),
    _s("D1_process","ZombieSshSession","warning","infra","alertmanager","zombie_processes","network_interfaces","tcp_connections","ssh zombie sessions","broken ssh keepalive","fix keepalive, prune sessions"),
    _s("D1_process","CronJobDatabaseConn","warning","jobs","alertmanager","cron_jobs","mysql_health","disk_usage","cron DB conn fail","DB connection limit hit","increase max_connections or reduce cron concurrency"),
    _s("D1_process","ProcessHung","critical","prod","alertmanager","zombie_processes","tcp_connections","disk_usage","process hung","lock contention","trace with strace, fix deadlock"),
    _s("D1_process","CronJobNFSMount","warning","jobs","alertmanager","cron_jobs","storage_nfs","disk_usage","cron NFS mount fail","NFS server unreachable","check NFS server, add soft mount option"),
    _s("D1_process","OOMKillLogAggregator","warning","infra","prometheus","oom_events","disk_usage","swap_usage","log aggregator OOM","large log burst","add buffer limit, rotate logs"),
    _s("D1_process","ZombieAfterExec","warning","infra","alertmanager","zombie_processes","systemd_units","cron_jobs","zombie after exec fail","exec path wrong","fix ExecStart path"),
    _s("D1_process","CronJobOutputRedirect","warning","jobs","alertmanager","cron_jobs","disk_usage","lvm_volumes","cron output fills disk","no output redirect","redirect to /dev/null or logrotate"),
    _s("D1_process","MemOvercommitKill","critical","prod","prometheus","oom_events","swap_usage","systemd_units","overcommit OOM","vm.overcommit_ratio too high","tune overcommit, add swap"),
    _s("D1_process","CronJobEnvMissing","warning","jobs","alertmanager","cron_jobs","disk_usage","systemd_units","cron env missing","cron env stripped","set SHELL and PATH in crontab"),
    _s("D1_process","ZombieAfterFork","warning","infra","alertmanager","zombie_processes","disk_usage","systemd_units","zombie after fork","double-fork pattern missing","implement double-fork or use daemon()"),
    _s("D1_process","ProcessRLimitNproc","critical","prod","prometheus","zombie_processes","oom_events","systemd_units","nproc limit hit","thread leak","fix thread leak, raise nproc"),
]

# ---------------------------------------------------------------------------
# D2 — Storage (40 scenarios)
# ---------------------------------------------------------------------------
D2 = [
    _s("D2_storage","DiskFullProduction","critical","prod","prometheus","disk_usage","lvm_volumes","storage_nfs","disk full genuine","log spiral fill","logrotate, clean old backups"),
    _s("D2_storage","InodeFull","critical","prod","prometheus","disk_usage","cron_jobs","lvm_volumes","inode exhausted","millions of small files","clean small files, add inodes"),
    _s("D2_storage","NFSStaleMount","critical","prod","alertmanager","storage_nfs","network_interfaces","disk_usage","NFS stale genuine","NFS server rebooted","remount NFS, check server health"),
    _s("D2_storage","RAIDDegraded","critical","infra","alertmanager","raid_mdadm","disk_usage","lvm_volumes","RAID degraded genuine","disk failure","replace failed disk, mdadm --add"),
    _s("D2_storage","LVMPVFailed","critical","infra","alertmanager","lvm_volumes","disk_usage","raid_mdadm","LVM PV failed","underlying disk error","pvmove data, replace disk"),
    _s("D2_storage","SwapExhausted","critical","prod","prometheus","swap_usage","oom_events","disk_usage","swap genuine exhausted","mem leak + swap use","add swap, fix leak"),
    _s("D2_storage","DiskSlowIO","critical","prod","prometheus","disk_usage","raid_mdadm","lvm_volumes","disk slow IO genuine","RAID rebuild I/O impact","throttle rebuild, migrate workload"),
    _s("D2_storage","NFSTimeout","critical","prod","alertmanager","storage_nfs","network_interfaces","tcp_connections","NFS timeout genuine","switch flap","fix switch, increase NFS timeo="),
    _s("D2_storage","LVMSnapshotFull","warning","infra","prometheus","lvm_volumes","disk_usage","swap_usage","LVM snapshot full","COW overflow","extend snapshot LV, rerun backup"),
    _s("D2_storage","RAIDRebuildFail","critical","infra","alertmanager","raid_mdadm","disk_usage","lvm_volumes","RAID rebuild fail","bad sector on spare","replace spare disk, start rebuild"),
    _s("D2_storage","DiskErrorSectors","critical","infra","prometheus","disk_usage","kernel_errors","raid_mdadm","bad sector IO error","aging HDD","backup data, replace disk"),
    _s("D2_storage","NFSPermissionDenied","warning","prod","alertmanager","storage_nfs","network_interfaces","disk_usage","NFS permission genuine","export options changed","fix /etc/exports, exportfs -ra"),
    _s("D2_storage","LVMExtendFailed","critical","infra","alertmanager","lvm_volumes","disk_usage","storage_nfs","LVM extend fail","VG has no free PE","add PV or clean up LVs"),
    _s("D2_storage","RAIDSplitBrain","critical","infra","alertmanager","raid_mdadm","network_interfaces","disk_usage","RAID split brain","network partition during resync","resolve manually, choose authoritative"),
    _s("D2_storage","SwapHighPressure","warning","prod","prometheus","swap_usage","oom_events","disk_usage","swap pressure genuine","working set > RAM","add RAM or tune vm.swappiness"),
    _s("D2_storage","DiskFullTmpfs","critical","prod","prometheus","disk_usage","swap_usage","lvm_volumes","tmpfs full","large /tmp usage","clean /tmp, increase tmpfs size"),
    _s("D2_storage","NFSClientHang","critical","prod","alertmanager","storage_nfs","network_interfaces","kernel_errors","NFS client hung","hard mount + server down","switch to soft+intr mount"),
    _s("D2_storage","LVMCacheCorrupt","critical","infra","alertmanager","lvm_volumes","disk_usage","kernel_errors","LVM cache corrupt","lvmcache device failure","remove cache device, restore from backup"),
    _s("D2_storage","RAIDArrayMissing","critical","infra","alertmanager","raid_mdadm","kernel_errors","disk_usage","RAID array missing","disk offline","bring disk online, mdadm --assemble"),
    _s("D2_storage","DiskOverprovision","warning","prod","prometheus","disk_usage","lvm_volumes","swap_usage","disk overprovision genuine","thin provisioning exceeded","add physical capacity"),
    _s("D2_storage","NFSLatencySpike","warning","prod","prometheus","storage_nfs","network_interfaces","tcp_connections","NFS latency genuine","NFS server overload","tune rsize/wsize, add NFS server"),
    _s("D2_storage","LVMVGCorruption","critical","infra","alertmanager","lvm_volumes","kernel_errors","disk_usage","VG corruption","IO error on PV metadata","vgck, restore from backup"),
    _s("D2_storage","RAIDWriteIntent","warning","infra","prometheus","raid_mdadm","disk_usage","lvm_volumes","RAID bitmap misaligned","bitmap block size mismatch","recreate bitmap with correct block"),
    _s("D2_storage","SwapDiskFull","critical","prod","prometheus","swap_usage","disk_usage","lvm_volumes","swap partition full","swap on same partition","add dedicated swap partition"),
    _s("D2_storage","DiskJournalFull","critical","prod","prometheus","disk_usage","kernel_errors","lvm_volumes","ext4 journal full","journal checkpoint slow","tune journal, run e2fsck"),
    _s("D2_storage","NFSExportGone","critical","prod","alertmanager","storage_nfs","network_interfaces","disk_usage","NFS export removed","export deleted","restore export, exportfs -ra"),
    _s("D2_storage","LVMThinPoolFull","critical","prod","prometheus","lvm_volumes","disk_usage","storage_nfs","thin pool full","thin volumes over-allocated","extend thin pool or deactivate LVs"),
    _s("D2_storage","RAIDResyncSlow","warning","infra","prometheus","raid_mdadm","disk_usage","kernel_errors","RAID resync slow","speed_limit_min too low","echo 100000 > /sys/block/md*/sync_speed_min"),
    _s("D2_storage","DiskAtaError","critical","infra","prometheus","disk_usage","kernel_errors","raid_mdadm","ATA error genuine","SATA cable failure","replace cable/disk, check SMART"),
    _s("D2_storage","NFSMountLoopback","warning","infra","alertmanager","storage_nfs","network_interfaces","disk_usage","NFS loopback mount","server mounting itself","remove loopback export"),
    _s("D2_storage","LVMActiveNode","critical","infra","alertmanager","lvm_volumes","disk_usage","network_interfaces","LVM activation conflict","clustered LVM lock conflict","use lvmlockd, fix cluster fencing"),
    _s("D2_storage","SwapZram","warning","prod","prometheus","swap_usage","oom_events","kernel_errors","zram swap exhausted","zram too small","increase zram size or add disk swap"),
    _s("D2_storage","DiskSectorRealloc","critical","infra","prometheus","disk_usage","kernel_errors","raid_mdadm","sector realloc accumulating","pending sectors","backup, replace disk"),
    _s("D2_storage","NFSIdmapd","warning","prod","alertmanager","storage_nfs","network_interfaces","disk_usage","NFS idmapd error","uid mismatch","fix idmapd.conf, restart idmapd"),
    _s("D2_storage","LVMMetadataFull","critical","infra","alertmanager","lvm_volumes","disk_usage","kernel_errors","LVM metadata area full","too many snapshots","remove old snapshots, extend metadata"),
    _s("D2_storage","RAIDChecksumError","critical","infra","prometheus","raid_mdadm","kernel_errors","disk_usage","RAID checksum mismatch","silent data corruption","run scrub, replace disk"),
    _s("D2_storage","DiskSmartFailing","critical","infra","prometheus","disk_usage","kernel_errors","raid_mdadm","SMART prefailure","reallocated sector count high","backup, replace disk urgently"),
    _s("D2_storage","NFSRPCTimeout","critical","prod","alertmanager","storage_nfs","network_interfaces","kernel_errors","NFS RPC timeout","portmapper blocked","check firewall, restart rpcbind"),
    _s("D2_storage","SwapOnSSD","warning","prod","prometheus","swap_usage","disk_usage","kernel_errors","SSD swap wear","heavy swap on SSD","add DRAM, reduce swappiness"),
    _s("D2_storage","DiskDevmapperStale","critical","infra","alertmanager","disk_usage","lvm_volumes","kernel_errors","device mapper stale","dm table not updated","dmsetup remove + reassemble"),
]

# ---------------------------------------------------------------------------
# D3 — Network (35 scenarios)
# ---------------------------------------------------------------------------
D3 = [
    _s("D3_network","NetworkInterfaceDown","critical","infra","alertmanager","network_interfaces","tcp_connections","dns_resolution","interface down genuine","cable unplugged","check cable, bring interface up"),
    _s("D3_network","DNSResolutionFail","critical","prod","prometheus","dns_resolution","network_interfaces","tcp_connections","DNS failure genuine","resolver unreachable","check /etc/resolv.conf, restart resolver"),
    _s("D3_network","TCPConnectionSaturation","critical","prod","prometheus","tcp_connections","network_interfaces","dns_resolution","TCP saturation genuine","connection leak","fix conn pool, increase ulimit"),
    _s("D3_network","NetworkPacketLoss","critical","prod","prometheus","network_interfaces","kernel_errors","tcp_connections","packet loss genuine","NIC driver bug","update driver, replace NIC"),
    _s("D3_network","ARPCacheExhausted","warning","prod","prometheus","network_interfaces","tcp_connections","kernel_errors","ARP cache exhausted","too many /32 routes","increase arp_neigh_gc_thresh"),
    _s("D3_network","DNSNdots","warning","prod","alertmanager","dns_resolution","network_interfaces","tcp_connections","DNS search domain loop","ndots=5 causing extra lookups","reduce ndots, use FQDN"),
    _s("D3_network","TCPTimeWaitExcess","warning","prod","prometheus","tcp_connections","network_interfaces","disk_usage","TIME_WAIT excess genuine","short-lived conn flood","enable tcp_tw_reuse, tune keepalive"),
    _s("D3_network","NetworkMTUMismatch","critical","prod","alertmanager","network_interfaces","kernel_errors","tcp_connections","MTU mismatch genuine","jumbo frames misconfigured","set MTU consistently across path"),
    _s("D3_network","DNSNegativeCache","warning","prod","prometheus","dns_resolution","network_interfaces","disk_usage","DNS negative cache storm","repeated NXDOMAIN","fix service discovery, cache tuning"),
    _s("D3_network","TCPSynFlood","critical","prod","alertmanager","tcp_connections","network_interfaces","kernel_errors","SYN flood genuine","DDoS attack","enable SYN cookies, rate limit"),
    _s("D3_network","NetworkQueueFull","critical","prod","prometheus","network_interfaces","kernel_errors","tcp_connections","TX queue overflow","NIC saturation","increase txqueuelen, upgrade NIC"),
    _s("D3_network","DNSServerDead","critical","prod","alertmanager","dns_resolution","network_interfaces","tcp_connections","DNS server dead","resolver crashed","restart resolver, add secondary"),
    _s("D3_network","TCPRetransmitHigh","warning","prod","prometheus","tcp_connections","network_interfaces","kernel_errors","retransmit storm","network congestion","check route, tune tcp_retries"),
    _s("D3_network","NetworkVLANMisconfigured","critical","infra","alertmanager","network_interfaces","kernel_errors","tcp_connections","VLAN misconfigured","VLAN ID mismatch","fix VLAN config on switch+host"),
    _s("D3_network","DNSSearchRotate","warning","prod","prometheus","dns_resolution","network_interfaces","disk_usage","DNS rotate not working","rotate option missing","add rotate to resolv.conf"),
    _s("D3_network","TCPKeepaliveTimeout","warning","prod","prometheus","tcp_connections","network_interfaces","dns_resolution","keepalive timeout genuine","idle conn closed by firewall","tune keepalive, fix firewall rule"),
    _s("D3_network","NetworkBondingFail","critical","infra","alertmanager","network_interfaces","kernel_errors","tcp_connections","bonding fail genuine","active slave failed","check bonding driver, replace NIC"),
    _s("D3_network","DNSZoneTransferFail","warning","infra","alertmanager","dns_resolution","network_interfaces","disk_usage","zone transfer fail","ACL blocking transfer","update allow-transfer ACL"),
    _s("D3_network","TCPFINWaitLeak","warning","prod","prometheus","tcp_connections","network_interfaces","kernel_errors","FIN_WAIT2 leak","connection not closed","fix close() in app, tune timeout"),
    _s("D3_network","NetworkEthtoolError","warning","infra","prometheus","network_interfaces","kernel_errors","disk_usage","NIC error counter rising","hardware fault","replace NIC, check transceiver"),
    _s("D3_network","DNSCachePoison","critical","prod","alertmanager","dns_resolution","network_interfaces","tcp_connections","DNS cache poisoned","DNSSEC not enforced","enable DNSSEC, flush cache"),
    _s("D3_network","TCPPortExhausted","critical","prod","prometheus","tcp_connections","network_interfaces","disk_usage","ephemeral port exhausted","ip_local_port_range too narrow","widen port range, fix conn leak"),
    _s("D3_network","NetworkFlapping","critical","infra","alertmanager","network_interfaces","kernel_errors","tcp_connections","interface flapping","cable/SFP fault","replace SFP/cable"),
    _s("D3_network","DNSForwarderLoop","warning","infra","alertmanager","dns_resolution","network_interfaces","disk_usage","forwarder loop","circular forwarding config","fix forwarder config"),
    _s("D3_network","TCPWindowScaling","warning","prod","prometheus","tcp_connections","network_interfaces","kernel_errors","window scaling disabled","middlebox stripping options","enable tcp_window_scaling, fix middlebox"),
    _s("D3_network","NetworkICMPBlocked","warning","infra","alertmanager","network_interfaces","kernel_errors","tcp_connections","ICMP blocked","firewall blocking PMTUD","allow ICMP type 3 code 4"),
    _s("D3_network","DNSEDNSFail","warning","prod","prometheus","dns_resolution","network_interfaces","disk_usage","EDNS failure","UDP 4096 blocked","add EDNS0 exception to firewall"),
    _s("D3_network","TCPAbortOnMemory","critical","prod","prometheus","tcp_connections","swap_usage","oom_events","TCP abort on memory genuine","socket buffer exhaustion","tune tcp_mem, add RAM"),
    _s("D3_network","NetworkMulticastFlood","warning","infra","prometheus","network_interfaces","kernel_errors","tcp_connections","multicast flood","IGMP snooping disabled","enable IGMP snooping on switch"),
    _s("D3_network","DNSSearchMismatch","warning","prod","alertmanager","dns_resolution","network_interfaces","disk_usage","search domain mismatch","k8s dnsPolicy wrong","set dnsPolicy: ClusterFirst"),
    _s("D3_network","TCPRSTFlood","critical","prod","alertmanager","tcp_connections","network_interfaces","kernel_errors","RST flood genuine","middlebox injecting RST","trace RST source, fix middlebox"),
    _s("D3_network","NetworkGRODisabled","warning","infra","prometheus","network_interfaces","kernel_errors","tcp_connections","GRO disabled","ethtool setting lost","ethtool -K <iface> gro on"),
    _s("D3_network","DNSNXDomainLoop","warning","prod","prometheus","dns_resolution","network_interfaces","disk_usage","NXDOMAIN loop","config error in service mesh","fix service mesh DNS config"),
    _s("D3_network","TCPConnRefused","critical","prod","alertmanager","tcp_connections","network_interfaces","disk_usage","connection refused genuine","service not listening","restart service, fix bind address"),
    _s("D3_network","NetworkTXDrops","critical","prod","prometheus","network_interfaces","kernel_errors","tcp_connections","TX drops genuine","qdisc limit","increase txqueuelen, tune qdisc"),
]

# ---------------------------------------------------------------------------
# D4 — Database (40 scenarios)
# ---------------------------------------------------------------------------
D4 = [
    _s("D4_database","MySQLReplicationLag","critical","db","prometheus","mysql_health","disk_usage","network_interfaces","MySQL replication lag genuine","large transaction lag","optimize slow query, enable parallel replication"),
    _s("D4_database","ProxySQLConnectionLimit","critical","db","alertmanager","proxysql_health","mysql_health","network_interfaces","ProxySQL limit genuine","conn pool exhausted","increase max_connections, fix conn leak"),
    _s("D4_database","MySQLSlowQuerySurge","warning","db","prometheus","mysql_health","disk_usage","storage_nfs","MySQL slow query genuine","missing index after schema change","add index, kill slow queries"),
    _s("D4_database","PostgreSQLReplication","critical","db","prometheus","postgresql_health","disk_usage","network_interfaces","PG replication genuine lag","WAL shipping timeout","fix WAL archive, check standby"),
    _s("D4_database","RedisOOM","critical","db","prometheus","redis_os_health","swap_usage","disk_usage","Redis OOM genuine","maxmemory exceeded","increase maxmemory or change policy"),
    _s("D4_database","MongoDBReplication","critical","db","alertmanager","mongodb_health","network_interfaces","disk_usage","MongoDB repl lag genuine","network partition","fix network, force re-sync"),
    _s("D4_database","MySQLTableFull","critical","db","prometheus","mysql_health","disk_usage","lvm_volumes","MySQL table full","innodb_data_file_path exhausted","add autoextend or new tablespace"),
    _s("D4_database","ProxySQLBackendDown","critical","db","alertmanager","proxysql_health","mysql_health","disk_usage","ProxySQL backend down genuine","MySQL server crashed","restart MySQL, check proxysql_servers"),
    _s("D4_database","PostgreSQLDiskFull","critical","db","prometheus","postgresql_health","disk_usage","lvm_volumes","PG disk full genuine","WAL accumulation","clean WAL, extend volume, vacuum"),
    _s("D4_database","RedisAOFFull","critical","db","prometheus","redis_os_health","disk_usage","lvm_volumes","Redis AOF disk full","AOF rewrite failed","extend disk, redis-cli bgrewriteaof"),
    _s("D4_database","MySQLDeadlock","warning","db","prometheus","mysql_health","tcp_connections","network_interfaces","MySQL deadlock spike","long transaction + lock contention","set innodb_lock_wait_timeout, fix app"),
    _s("D4_database","MongoDBOplogFull","critical","db","alertmanager","mongodb_health","disk_usage","lvm_volumes","MongoDB oplog full","oplog too small","increase oplogSizeMB, add disk"),
    _s("D4_database","ProxySQLQueryCacheFull","warning","db","prometheus","proxysql_health","mysql_health","disk_usage","ProxySQL query cache full","cache too small","increase query_cache_size or evict"),
    _s("D4_database","PostgreSQLVacuumFail","critical","db","alertmanager","postgresql_health","disk_usage","cron_jobs","PG vacuum fail genuine","autovacuum worker crash","manual VACUUM ANALYZE, fix autovacuum"),
    _s("D4_database","RedisClusterFail","critical","db","alertmanager","redis_os_health","network_interfaces","tcp_connections","Redis cluster fail genuine","quorum lost","fix network partition, add nodes"),
    _s("D4_database","MySQLGTIDGap","critical","db","alertmanager","mysql_health","network_interfaces","disk_usage","MySQL GTID gap genuine","skipped transaction","resolve GTID gap with correct binlog"),
    _s("D4_database","MongoDBAuthFail","critical","db","alertmanager","mongodb_health","network_interfaces","disk_usage","MongoDB auth fail genuine","keyfile mismatch","sync keyfiles, restart mongod"),
    _s("D4_database","PostgreSQLSSLFail","warning","db","alertmanager","postgresql_health","network_interfaces","disk_usage","PG SSL cert expired","cert not renewed","renew cert, reload pg_hba.conf"),
    _s("D4_database","RedisReplicationBreak","critical","db","alertmanager","redis_os_health","network_interfaces","tcp_connections","Redis replication break","network flap","REPLICAOF NO ONE + re-attach"),
    _s("D4_database","MySQLBinlogFull","critical","db","prometheus","mysql_health","disk_usage","lvm_volumes","MySQL binlog disk full","expire_logs_days=0","set binlog_expire_logs_seconds, purge"),
    _s("D4_database","ProxySQLLatencySpike","warning","db","prometheus","proxysql_health","mysql_health","network_interfaces","ProxySQL latency genuine","backend query slow","identify slow backend, tune query"),
    _s("D4_database","PostgreSQLConnectionExhausted","critical","db","prometheus","postgresql_health","tcp_connections","network_interfaces","PG connections exhausted","pgbouncer not sizing correctly","tune pool_size, max_client_conn"),
    _s("D4_database","MongoDBIndexBuild","warning","db","prometheus","mongodb_health","disk_usage","oom_events","MongoDB index build OOM","index build exceeds RAM","use rolling index build, increase RAM"),
    _s("D4_database","RedisKeyEviction","warning","db","prometheus","redis_os_health","swap_usage","disk_usage","Redis eviction storm","maxmemory-policy aggressive","tune policy, increase maxmemory"),
    _s("D4_database","MySQLInnoDB_LogFull","critical","db","prometheus","mysql_health","disk_usage","lvm_volumes","InnoDB log full","innodb_log_file_size too small","increase log file size, restart"),
    _s("D4_database","MongoDBWTCache","critical","db","prometheus","mongodb_health","swap_usage","disk_usage","MongoDB WT cache full","cache too small","tune cache_size_gb, add RAM"),
    _s("D4_database","ProxySQLUserAuthFail","critical","db","alertmanager","proxysql_health","mysql_health","network_interfaces","ProxySQL user auth fail","password rotation not synced","update mysql_users, load to runtime"),
    _s("D4_database","PostgreSQLXidWrap","critical","db","prometheus","postgresql_health","disk_usage","cron_jobs","PG XID wraparound","autovacuum not keeping up","VACUUM FREEZE on affected tables"),
    _s("D4_database","RedisSlowlog","warning","db","prometheus","redis_os_health","tcp_connections","network_interfaces","Redis slowlog spike","O(N) command on large key","refactor command, reduce key size"),
    _s("D4_database","MySQLUndoLog","critical","db","prometheus","mysql_health","disk_usage","lvm_volumes","MySQL undo log full","long-running transaction","kill long trx, increase undo tablespace"),
    _s("D4_database","MongoDBChunkMigration","warning","db","prometheus","mongodb_health","network_interfaces","disk_usage","MongoDB chunk migration fail","network interruption","resume migration, check balancer"),
    _s("D4_database","PostgreSQLLockWait","warning","db","prometheus","postgresql_health","tcp_connections","disk_usage","PG lock wait timeout genuine","DDL blocking DML","kill blocking DDL, schedule off-peak"),
    _s("D4_database","RedisRDB_Fail","warning","db","prometheus","redis_os_health","disk_usage","lvm_volumes","Redis RDB save fail","disk full during BGSAVE","free disk, redis-cli bgsave"),
    _s("D4_database","MySQLInnoDB_Corrupt","critical","db","alertmanager","mysql_health","disk_usage","kernel_errors","InnoDB corruption genuine","hardware IO error","restore from backup, replace disk"),
    _s("D4_database","MongoDBTTLIndex","warning","db","prometheus","mongodb_health","disk_usage","cron_jobs","MongoDB TTL index lagging","large doc delete backlog","allow TTL to catch up, add index hint"),
    _s("D4_database","ProxySQLMaxConnsPerHost","warning","db","prometheus","proxysql_health","mysql_health","disk_usage","ProxySQL per-host limit hit","max_connections_per_host too low","increase limit in mysql_servers"),
    _s("D4_database","PostgreSQLExtension","warning","db","alertmanager","postgresql_health","disk_usage","kernel_errors","PG extension crash","extension upgrade incompatible","pin extension version, restore"),
    _s("D4_database","RedisClusterSlotMigration","warning","db","prometheus","redis_os_health","network_interfaces","tcp_connections","Redis slot migration stuck","network instability","fix network, resume migration"),
    _s("D4_database","MySQLBinlogCorrupt","critical","db","alertmanager","mysql_health","disk_usage","kernel_errors","MySQL binlog corrupt","disk sector error","restore from backup, replace disk"),
    _s("D4_database","MongoDBElectionFail","critical","db","alertmanager","mongodb_health","network_interfaces","tcp_connections","MongoDB election fail genuine","network partition","fix partition, check priority"),
]

# ---------------------------------------------------------------------------
# D5 — Proxy/LB (25 scenarios)
# ---------------------------------------------------------------------------
D5 = [
    _s("D5_proxy_lb","HAProxyBackendDown","critical","infra","alertmanager","service_haproxy","mysql_health","network_interfaces","HAProxy backend down genuine","backend server crashed","restart backend, check health check"),
    _s("D5_proxy_lb","NginxUpstreamError","critical","prod","alertmanager","service_nginx","tcp_connections","disk_usage","Nginx upstream error genuine","upstream workers OOM","restart workers, increase memory"),
    _s("D5_proxy_lb","KeepalivedVIPLost","critical","infra","alertmanager","service_keepalived","network_interfaces","disk_usage","VIP loss genuine","keepalived daemon crash","restart keepalived, check VRRP"),
    _s("D5_proxy_lb","HAProxyQueueFull","warning","infra","prometheus","service_haproxy","tcp_connections","disk_usage","HAProxy queue overflow","maxconn too low","increase maxconn, add backend"),
    _s("D5_proxy_lb","NginxWorkerCrash","critical","prod","alertmanager","service_nginx","oom_events","disk_usage","Nginx worker crash genuine","worker_processes segfault","check logs, downgrade nginx"),
    _s("D5_proxy_lb","KeepalivedSplitBrain","critical","infra","alertmanager","service_keepalived","network_interfaces","tcp_connections","VRRP split brain genuine","network partition","fix partition, lower priority one node"),
    _s("D5_proxy_lb","HAProxySSLExpiry","critical","prod","alertmanager","service_haproxy","disk_usage","network_interfaces","HAProxy SSL cert expired","cert not renewed","renew cert, reload haproxy"),
    _s("D5_proxy_lb","NginxConfigError","critical","prod","alertmanager","service_nginx","disk_usage","cron_jobs","Nginx config error genuine","syntax error after deploy","nginx -t, rollback config"),
    _s("D5_proxy_lb","KeepalivedMulticast","warning","infra","prometheus","service_keepalived","network_interfaces","kernel_errors","VRRP multicast fail","IGMP blocked","allow VRRP multicast on switch"),
    _s("D5_proxy_lb","HAProxyDrainTimeout","warning","infra","alertmanager","service_haproxy","tcp_connections","disk_usage","HAProxy drain timeout","backend not draining in time","increase timeout_drain, fix backend"),
    _s("D5_proxy_lb","NginxGzipOOM","warning","prod","prometheus","service_nginx","oom_events","disk_usage","Nginx gzip OOM","gzip_buffers too large","reduce gzip_buffers, add memory"),
    _s("D5_proxy_lb","KeepalivedAuthFail","critical","infra","alertmanager","service_keepalived","network_interfaces","disk_usage","VRRP auth fail genuine","password mismatch","sync auth_pass across nodes"),
    _s("D5_proxy_lb","HAProxyHealthCheckFail","warning","infra","prometheus","service_haproxy","tcp_connections","mysql_health","HAProxy health check fail genuine","backend health check down","fix backend endpoint, restart"),
    _s("D5_proxy_lb","NginxRateLimitHit","warning","prod","prometheus","service_nginx","tcp_connections","disk_usage","Nginx rate limit genuine","burst exceeded","tune limit_req zone or raise burst"),
    _s("D5_proxy_lb","KeepalivedNotifyFail","warning","infra","alertmanager","service_keepalived","cron_jobs","disk_usage","keepalived notify script fail","script permission denied","fix script permissions, chmod +x"),
    _s("D5_proxy_lb","HAProxyLog_Full","warning","infra","prometheus","service_haproxy","disk_usage","lvm_volumes","HAProxy log disk full","no logrotate for haproxy","add logrotate, extend volume"),
    _s("D5_proxy_lb","NginxProxyTimeout","critical","prod","alertmanager","service_nginx","tcp_connections","network_interfaces","Nginx proxy timeout genuine","upstream too slow","increase proxy_read_timeout, fix upstream"),
    _s("D5_proxy_lb","KeepalivedPriorityLow","warning","infra","prometheus","service_keepalived","network_interfaces","disk_usage","VRRP priority issue","priority identical on nodes","adjust priority values"),
    _s("D5_proxy_lb","HAProxyStickyFail","warning","infra","alertmanager","service_haproxy","tcp_connections","disk_usage","HAProxy sticky session fail","server down with sticky clients","enable redispatch option"),
    _s("D5_proxy_lb","NginxSslHandshake","critical","prod","alertmanager","service_nginx","network_interfaces","disk_usage","Nginx SSL handshake fail","cipher mismatch","update ssl_ciphers, check client"),
    _s("D5_proxy_lb","KeepalivedTrackScript","critical","infra","alertmanager","service_keepalived","disk_usage","cron_jobs","track script failing","script returns 1","fix track script logic"),
    _s("D5_proxy_lb","HAProxyRetryLimit","warning","infra","prometheus","service_haproxy","tcp_connections","mysql_health","HAProxy retries exceeded","backend flapping","fix backend stability, tune retries"),
    _s("D5_proxy_lb","NginxUpstreamKeepalive","warning","prod","prometheus","service_nginx","tcp_connections","network_interfaces","Nginx upstream keepalive pool full","pool too small","increase keepalive in upstream block"),
    _s("D5_proxy_lb","KeepalivedInstanceConflict","critical","infra","alertmanager","service_keepalived","network_interfaces","kernel_errors","VRRP instance conflict","duplicate VRID on network","assign unique VRID"),
    _s("D5_proxy_lb","HAProxyFrontendLimit","critical","prod","prometheus","service_haproxy","tcp_connections","disk_usage","HAProxy frontend conn limit","maxconn on frontend too low","increase frontend maxconn"),
]

# ---------------------------------------------------------------------------
# D6 — Hardware (20 scenarios)
# ---------------------------------------------------------------------------
D6 = [
    _s("D6_hardware","MCEUncorrectable","critical","infra","alertmanager","memory_hw_errors","kernel_errors","disk_usage","MCE uncorrectable genuine","DIMM failure","replace DIMM, offline node"),
    _s("D6_hardware","KernelPanic","critical","infra","alertmanager","kernel_errors","memory_hw_errors","disk_usage","kernel panic genuine","hardware fault","boot from rescue, check dmesg/mcelog"),
    _s("D6_hardware","ECCCorrectableHigh","warning","infra","prometheus","memory_hw_errors","kernel_errors","disk_usage","ECC correctable accumulating","aging DIMM","monitor, plan replacement"),
    _s("D6_hardware","NMIWatchdog","critical","infra","alertmanager","kernel_errors","memory_hw_errors","disk_usage","NMI watchdog fired","hardware lockup","offline node, hardware diagnostics"),
    _s("D6_hardware","CPUMachineCheck","critical","infra","alertmanager","kernel_errors","memory_hw_errors","disk_usage","CPU machine check genuine","CPU microcode bug","update microcode, replace CPU"),
    _s("D6_hardware","PCIeError","critical","infra","alertmanager","kernel_errors","disk_usage","network_interfaces","PCIe error genuine","NIC/HBA hardware fault","reseat card, replace if persistent"),
    _s("D6_hardware","DIMMLinkError","critical","infra","alertmanager","memory_hw_errors","kernel_errors","disk_usage","DIMM link error","loose DIMM","reseat DIMM, replace if persistent"),
    _s("D6_hardware","HardwareThermalThrottle","warning","infra","prometheus","kernel_errors","memory_hw_errors","disk_usage","CPU thermal throttle genuine","airflow blocked","clean filters, check cooling"),
    _s("D6_hardware","DiskSMARTFail","critical","infra","prometheus","disk_usage","kernel_errors","memory_hw_errors","SMART prefailure genuine","reallocated sectors high","backup data, replace disk"),
    _s("D6_hardware","MemorySingleBitECC","warning","infra","prometheus","memory_hw_errors","kernel_errors","disk_usage","single bit ECC corrected","DIMM degrading","schedule replacement at next window"),
    _s("D6_hardware","PowerSupplyFail","critical","infra","alertmanager","kernel_errors","memory_hw_errors","network_interfaces","power supply fail genuine","PSU fault","replace PSU, check redundancy"),
    _s("D6_hardware","FanFailure","critical","infra","alertmanager","kernel_errors","memory_hw_errors","disk_usage","fan failure genuine","fan bearing failure","replace fan, monitor temps"),
    _s("D6_hardware","MCEBank1Error","critical","infra","alertmanager","memory_hw_errors","kernel_errors","disk_usage","MCE bank1 genuine","L1/L2 cache error","offline CPU, run mprime test"),
    _s("D6_hardware","NUMAImbalance","warning","infra","prometheus","memory_hw_errors","kernel_errors","swap_usage","NUMA imbalance genuine","workload pinned to one NUMA","tune numactl, enable numa_balancing"),
    _s("D6_hardware","TPM_PCRMismatch","critical","infra","alertmanager","kernel_errors","disk_usage","memory_hw_errors","TPM PCR mismatch","boot path changed","verify boot chain, reset PCR"),
    _s("D6_hardware","HyperThreadingDisabled","warning","infra","alertmanager","kernel_errors","memory_hw_errors","disk_usage","HT disabled by CPU bug","Spectre/MDS mitigation","enable SMT if risk accepted"),
    _s("D6_hardware","RDMALinkDown","critical","infra","alertmanager","network_interfaces","kernel_errors","memory_hw_errors","RDMA link down genuine","IB cable fault","replace cable, check HCA"),
    _s("D6_hardware","CPUOverheat","critical","infra","alertmanager","kernel_errors","memory_hw_errors","disk_usage","CPU overheat genuine","thermal paste dried","replace thermal paste, clean heatsink"),
    _s("D6_hardware","MemoryRowHammer","critical","infra","alertmanager","memory_hw_errors","kernel_errors","disk_usage","rowhammer detected","DDR4 without TRR","enable kernel mitigation, replace DIMM"),
    _s("D6_hardware","PCIeAER","warning","infra","prometheus","kernel_errors","disk_usage","network_interfaces","PCIe AER warning","connector oxidation","clean connector, replace if persistent"),
]

# ---------------------------------------------------------------------------
# D7 — Container (20 scenarios)
# ---------------------------------------------------------------------------
D7 = [
    _s("D7_container","DockerDaemonCrash","critical","infra","alertmanager","docker_daemon","containerd_state","disk_usage","Docker daemon crash genuine","daemon OOM","restart dockerd, check logs"),
    _s("D7_container","ContainerdShimLeak","warning","infra","prometheus","containerd_state","docker_daemon","disk_usage","containerd shim leak genuine","shim not cleaned up","restart containerd, upgrade version"),
    _s("D7_container","DockerOverlayFull","critical","prod","prometheus","docker_daemon","disk_usage","lvm_volumes","overlay storage full genuine","dangling images","docker system prune, extend volume"),
    _s("D7_container","ContainerdCNIFail","critical","prod","alertmanager","containerd_state","network_interfaces","dns_resolution","CNI plugin fail genuine","CNI binary missing","reinstall CNI plugin"),
    _s("D7_container","DockerRegistryAuth","critical","prod","alertmanager","docker_daemon","network_interfaces","disk_usage","registry auth fail genuine","imagePullSecret expired","rotate pull secret"),
    _s("D7_container","ContainerdSnapshotCorrupt","critical","infra","alertmanager","containerd_state","disk_usage","kernel_errors","snapshot corruption genuine","unclean shutdown","ctr snapshot rm, reimport image"),
    _s("D7_container","DockerNetworkNamespace","critical","prod","alertmanager","docker_daemon","network_interfaces","kernel_errors","network namespace leak","veth not cleaned","restart docker, delete stale netns"),
    _s("D7_container","ContainerdGRPCTimeout","critical","prod","alertmanager","containerd_state","docker_daemon","disk_usage","containerd gRPC timeout genuine","containerd frozen","restart containerd, check load"),
    _s("D7_container","DockerVolumeOrphan","warning","infra","prometheus","docker_daemon","disk_usage","lvm_volumes","volume orphan accumulation","containers deleted without pruning","docker volume prune"),
    _s("D7_container","ContainerdMetadataDB","critical","infra","alertmanager","containerd_state","disk_usage","kernel_errors","bolt metadata DB corrupt","power failure during write","restore bolt.db from backup"),
    _s("D7_container","DockerSeccompDenied","warning","prod","alertmanager","docker_daemon","kernel_errors","network_interfaces","seccomp violation genuine","syscall not in whitelist","add syscall to seccomp profile"),
    _s("D7_container","ContainerdPullTimeout","warning","prod","alertmanager","containerd_state","network_interfaces","disk_usage","image pull timeout genuine","registry slow","increase pull timeout, use local mirror"),
    _s("D7_container","DockerIPTablesConflict","critical","infra","alertmanager","docker_daemon","network_interfaces","kernel_errors","iptables conflict genuine","custom rules conflicting","merge iptables rules, restart docker"),
    _s("D7_container","ContainerdCgroupMigration","warning","infra","alertmanager","containerd_state","kernel_errors","systemd_units","cgroup v1→v2 migration","containerd cgroup driver mismatch","set SystemdCgroup=true in config"),
    _s("D7_container","DockerDNSFail","warning","prod","alertmanager","docker_daemon","dns_resolution","network_interfaces","Docker DNS fail genuine","embedded DNS overloaded","use host DNS, increase ndots"),
    _s("D7_container","ContainerdOOMKill","critical","prod","prometheus","containerd_state","oom_events","swap_usage","container OOM genuine","limits too low","increase container memory limit"),
    _s("D7_container","DockerSwarmLeaderElection","critical","infra","alertmanager","docker_daemon","network_interfaces","tcp_connections","Swarm election fail","quorum lost","restore quorum, check network"),
    _s("D7_container","ContainerdEventLoop","warning","infra","prometheus","containerd_state","docker_daemon","kernel_errors","containerd event loop blocked","goroutine deadlock","restart containerd, file bug"),
    _s("D7_container","DockerBridgeConflict","warning","infra","alertmanager","docker_daemon","network_interfaces","disk_usage","bridge IP conflict","172.17.0.0/16 conflict","change docker0 subnet"),
    _s("D7_container","ContainerdLeaseExpiry","warning","infra","prometheus","containerd_state","disk_usage","cron_jobs","lease expiry GC miss","GC not running","restart containerd GC, clear leases"),
]

# ---------------------------------------------------------------------------
# Assemble all 1000 pairs
# ---------------------------------------------------------------------------

ALL_SCENARIOS = D0 + D1 + D2 + D3 + D4 + D5 + D6 + D7  # 40+30+40+35+40+25+20+20 = 250


def build_all_pairs() -> list[dict]:
    pairs: list[dict] = []
    for scenario in ALL_SCENARIOS:
        pairs.extend(scenario)
    return pairs


# ---------------------------------------------------------------------------
# Main ingest
# ---------------------------------------------------------------------------

async def ingest(redis_url: str, ollama_url: str | None) -> None:
    ws = WorkerSettings()
    if ollama_url:
        ws = ws.model_copy(update={"ollama_base_url": ollama_url})

    embed_model = getattr(ws, "embed_model", "nomic-embed-text:latest")
    ollama_base = getattr(ws, "ollama_base_url", "http://localhost:11434")

    llm = build_llm_client(base_url=ollama_base)
    r = await aioredis.from_url(redis_url, decode_responses=False)
    rag_cfg = PostgresRAGSettings()
    store = RedisVectorStore(r, rag_cfg)

    pairs = build_all_pairs()
    total = len(pairs)
    logger.info("ingest_os_hard_fail_rag: total_pairs=%d collection=%s", total, COLLECTION_OS_HARD_FAIL_DIAGNOSTIC)

    ingested = 0
    for i in range(0, total, BATCH_SIZE):
        batch = pairs[i: i + BATCH_SIZE]
        texts = [p["text"] for p in batch]
        try:
            resp = await llm.embed(model=embed_model, input=texts, keep_alive="10m")
            vecs = [_pad_vec(v) for v in _vecs_from_embed_response(resp)]
        except Exception as exc:
            logger.error("embed batch failed i=%d err=%r", i, exc)
            raise

        if len(vecs) != len(texts):
            raise RuntimeError(f"embed mismatch: want {len(texts)} got {len(vecs)}")

        points = [
            PointStruct(
                id=_point_id(t),
                vector=v,
                payload=p,
            )
            for t, v, p in zip(texts, vecs, batch)
        ]
        await store.upsert(COLLECTION_OS_HARD_FAIL_DIAGNOSTIC, points)
        ingested += len(points)
        logger.info("progress: %d/%d", ingested, total)

    logger.info("ingest_os_hard_fail_rag: done ingested=%d", ingested)
    await r.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest os_hard_fail_diagnostic RAG collection")
    parser.add_argument("--redis-url", default="redis://localhost:16379/0")
    parser.add_argument("--ollama-url", default=None)
    args = parser.parse_args()
    asyncio.run(ingest(args.redis_url, args.ollama_url))


if __name__ == "__main__":
    main()
