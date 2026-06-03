#!/usr/bin/env python3
"""Generate SYS_HARD_FAIL RAG advisory pairs for OS-level failures.

Covers gaps in existing dataset: systemd services, disk/NFS, database,
HAProxy, OS OOM, network — all at the OS/VM layer (not K8s layer).
Writes to data/rag_training/sys_hard_fail_os_advisory_pairs.jsonl
"""
import json
import pathlib

NS = "multi-agent"
OUT = pathlib.Path(__file__).parent.parent / "data" / "rag_training" / "sys_hard_fail_os_advisory_pairs.jsonl"

RECORDS = []

def add(alert_id, alertname, summary, evidence_lines, root_cause, steps, remediations, severity="critical"):
    RECORDS.append({
        "alert_id": alert_id,
        "lane": "SYS_HARD_FAIL",
        "alert_context": {
            "alertname": alertname,
            "namespace": NS,
            "severity": severity,
            "labels": {"namespace": NS},
            "annotations": {"summary": summary[:120]},
        },
        "evidence": evidence_lines,
        "root_cause": root_cause,
        "verification_steps": steps,
        "proposed_remediation": remediations,
    })

# ── SYSTEMD SERVICE FAILURES ──────────────────────────────────────────────
SERVICES = [
    ("nginx", "NginxServiceDown", "Nginx web server unit failed"),
    ("mysql", "MySQLServiceDown", "MySQL database service unit failed"),
    ("postgresql", "PostgreSQLServiceDown", "PostgreSQL service unit failed"),
    ("redis", "RedisServiceDown", "Redis in-memory store service failed"),
    ("kafka", "KafkaServiceDown", "Kafka broker systemd unit failed"),
    ("haproxy", "HAProxyServiceDown", "HAProxy load balancer service unit failed"),
    ("elasticsearch", "ElasticsearchServiceDown", "Elasticsearch service unit failed"),
    ("mongod", "MongoDBServiceDown", "MongoDB daemon service unit failed"),
    ("rabbitmq", "RabbitMQServiceDown", "RabbitMQ broker service unit failed"),
    ("docker", "DockerServiceDown", "Docker daemon service unit failed"),
]
for i, (svc, alertname, summary) in enumerate(SERVICES, start=1):
    pid = f"sop-OS-SYSTEMD-{i:04d}"
    add(
        pid, alertname, summary,
        [
            f"systemd_units probe: critical_failed_units=['{svc}'] result=FAILED. "
            f"`systemctl status {svc}` → ActiveState=failed MainPID=0 SubState=dead. "
            f"journalctl -u {svc} --since '5 min ago' shows exit-code or signal.",
        ],
        f"Systemd unit {svc} transitioned to failed state. "
        f"Root cause requires log inspection: OOM kill, config error, or dependency missing.",
        [
            {"layer": "L1", "command": f"systemctl status {svc}", "rationale": f"Confirm {svc} unit state"},
            {"layer": "L1", "command": f"journalctl -u {svc} -n 50 --no-pager", "rationale": "Last 50 log lines for failure reason"},
            {"layer": "L1", "command": f"systemctl list-dependencies {svc} | head -20", "rationale": "Check if a dependency failed first"},
        ],
        [
            {"step": f"systemctl restart {svc}", "approval_required": False},
            {"step": f"If restart fails: journalctl -u {svc} -n 100 to identify root cause before retry", "approval_required": False},
            {"step": f"If config error: validate config and reload — e.g., nginx -t / mysqld --validate-config", "approval_required": False},
        ],
    )

# ── SYSTEMD CRASH LOOP ────────────────────────────────────────────────────
for i, (svc, alertname, summary) in enumerate(SERVICES[:5], start=11):
    pid = f"sop-OS-SYSTEMD-{i:04d}"
    add(
        pid, f"{alertname}CrashLoop", f"{summary} — start-limit-hit crash loop",
        [
            f"systemd_units probe: critical_failed_units=['{svc}'] result=FAILED. "
            f"`systemctl status {svc}` → Result: start-limit-hit. StartLimitBurst exceeded. "
            f"Unit will NOT auto-restart. Manual intervention required.",
        ],
        f"Systemd unit {svc} hit start-limit (repeated rapid restarts). "
        f"Service is locked in failed state and will not auto-recover without reset.",
        [
            {"layer": "L1", "command": f"systemctl status {svc}", "rationale": "Confirm start-limit-hit state"},
            {"layer": "L1", "command": f"journalctl -u {svc} -n 100 --no-pager", "rationale": "Find why service exits immediately"},
        ],
        [
            {"step": f"systemctl reset-failed {svc}", "approval_required": False},
            {"step": f"Fix underlying cause (config, port conflict, missing file)", "approval_required": False},
            {"step": f"systemctl start {svc}", "approval_required": False},
        ],
    )

# ── DISK FULL ─────────────────────────────────────────────────────────────
DISK_CASES = [
    ("DiskFull", "/var/log", "Log partition disk full", 98, "Log rotation failed or logging volume undersized."),
    ("DiskFull", "/data", "Data partition disk full", 97, "Database or application data growth exceeded partition capacity."),
    ("DiskFull", "/", "Root filesystem disk full", 96, "Root filesystem exhausted — may be caused by runaway process writing to /tmp or log dirs."),
    ("DiskFull", "/var/lib/docker", "Docker data partition full", 99, "Docker images/layers or container logs consuming all space."),
    ("InodeExhausted", "/var/spool", "Inode exhaustion on spool partition", 100, "Small-file spam (e.g., cron outputs, mail queue) exhausted inodes before disk space."),
]
for i, (alertname, mount, summary, pct, root_cause) in enumerate(DISK_CASES, start=1):
    pid = f"sop-OS-DISK-{i:04d}"
    add(
        pid, alertname, summary,
        [
            f"disk_usage probe: critical_partitions=['{mount}({pct}%)'] result=FAILED. "
            f"`df -h {mount}` → Use%={pct}% Avail=0. "
            + (f"`df -i {mount}` → IUse%=100%" if "Inode" in alertname else ""),
        ],
        root_cause,
        [
            {"layer": "L1", "command": f"df -h {mount}", "rationale": f"Confirm usage on {mount}"},
            {"layer": "L1", "command": f"du -sh {mount}/* 2>/dev/null | sort -rh | head -20", "rationale": "Find largest consumers"},
            {"layer": "L1", "command": f"find {mount} -name '*.log' -mtime +7 -size +100M 2>/dev/null | head -10", "rationale": "Find old large log files"},
        ],
        [
            {"step": f"find {mount} -name '*.log' -mtime +7 -delete (review first)", "approval_required": True},
            {"step": "Increase partition size or add storage if growth is legitimate", "approval_required": True},
            {"step": "Configure log rotation: logrotate -f /etc/logrotate.conf", "approval_required": False},
        ],
    )

# ── NFS FAILURES ─────────────────────────────────────────────────────────
NFS_CASES = [
    ("NFSStaleMount", "/mnt/nfs-data", "NFS mount stale handle — I/O operations hanging"),
    ("NFSUnreachable", "/mnt/shared", "NFS server unreachable — mount point inaccessible"),
    ("NFSTimeoutIO", "/mnt/nfs-backup", "NFS I/O timeout — backup writes stalling"),
]
for i, (alertname, mount, summary) in enumerate(NFS_CASES, start=1):
    pid = f"sop-OS-NFS-{i:04d}"
    add(
        pid, alertname, summary,
        [
            f"storage_nfs probe: nfs_error_count>0 stale_mounts=['{mount}'] result=FAILED. "
            f"`stat {mount}` hangs or returns ESTALE. NFS server may be unreachable.",
        ],
        f"NFS mount {mount} has stale handle or server connectivity issue. "
        "Processes waiting on I/O to this mount will block until remount or server recovery.",
        [
            {"layer": "L1", "command": f"mountpoint -q {mount} && echo mounted || echo not_mounted", "rationale": f"Check if {mount} is mounted"},
            {"layer": "L1", "command": f"timeout 5 stat {mount} 2>&1 || echo 'TIMEOUT/STALE'", "rationale": "Detect stale handle"},
            {"layer": "L1", "command": "showmount -e <nfs-server-ip>", "rationale": "Verify NFS server exports are reachable"},
        ],
        [
            {"step": f"umount -l {mount}  # lazy unmount for stale handle", "approval_required": False},
            {"step": f"mount {mount}  # remount from fstab", "approval_required": False},
            {"step": "If server unreachable: check NFS server health and network path", "approval_required": False},
        ],
    )

# ── MYSQL / DATABASE ──────────────────────────────────────────────────────
MYSQL_CASES = [
    ("MySQLMaxConnections", "MySQL max_connections hit — new connections refused",
     "threads_connected >= max_connections (151). New connection attempts fail with 'Too many connections'.",
     "Connection pool exhausted. Applications unable to connect. Queries queuing upstream.",
     "Increase max_connections or reduce connection pool size in applications."),

    ("MySQLReplicationStopped", "MySQL replication SQL thread stopped — replica lag growing",
     "Slave_SQL_Running=No. Last_Error='Could not execute Write_rows_event'. Seconds_Behind_Master=3847.",
     "MySQL replication SQL thread stopped due to a row-format conflict or duplicate key. Replica data diverging from master.",
     "STOP SLAVE; SET GLOBAL SQL_SLAVE_SKIP_COUNTER=1; START SLAVE; — only after confirming safe to skip."),

    ("MySQLDeadlock", "MySQL deadlock surge — transactions rolling back",
     "innodb_lock_waits=47 innodb_deadlocks_last_10min=38. Slow query log: 15 queries > 10s.",
     "Deadlock surge from concurrent transactions accessing same rows in different order. Applications see rollback errors.",
     "Identify hot rows via SHOW ENGINE INNODB STATUS; restructure transaction order in application."),
]
for i, (alertname, summary, evidence_text, root_cause, fix) in enumerate(MYSQL_CASES, start=1):
    pid = f"sop-OS-MYSQL-{i:04d}"
    add(
        pid, alertname, summary,
        [f"mysql_health probe: result=FAILED anomalies=[...]. {evidence_text}"],
        root_cause,
        [
            {"layer": "L1", "command": "mysql -u root -e \"SHOW STATUS LIKE 'Threads_connected'\"", "rationale": "Live connection count"},
            {"layer": "L1", "command": "mysql -u root -e \"SHOW PROCESSLIST\" | head -30", "rationale": "Active queries and lock waits"},
            {"layer": "L1", "command": "mysql -u root -e \"SHOW SLAVE STATUS\\G\" | grep -E 'Running|Behind|Error'", "rationale": "Replication state"},
        ],
        [{"step": fix, "approval_required": True}],
    )

# ── PROXYSQL ──────────────────────────────────────────────────────────────
add(
    "sop-OS-PROXYSQL-0001",
    "ProxySQLClientOverload",
    "ProxySQL client connection limit approaching — queries being queued",
    [
        "proxysql_health probe: result=FAILED anomalies=['proxysql_clients=1987>threshold']. "
        "ProxySQL Admin: SELECT * FROM stats_mysql_global WHERE Variable_Name='Client_Connections_connected' → 1987. "
        "mysql_connections_queued growing."
    ],
    "ProxySQL client connection ceiling nearly reached. "
    "New connections are being queued or rejected depending on max_connections config.",
    [
        {"layer": "L1", "command": "mysql -h 127.0.0.1 -P 6032 -u radmin -pradmin -e \"SELECT * FROM stats_mysql_global WHERE Variable_Name LIKE 'Client%'\"", "rationale": "ProxySQL connection stats"},
        {"layer": "L1", "command": "mysql -h 127.0.0.1 -P 6032 -u radmin -pradmin -e \"SELECT hostgroup,srv_host,status,ConnUsed,ConnFree FROM stats_mysql_connection_pool\"", "rationale": "Backend connection pool state"},
    ],
    [
        {"step": "Reduce upstream application connection pool size to free ProxySQL slots", "approval_required": False},
        {"step": "mysql -h 127.0.0.1 -P 6032 -u radmin -pradmin -e \"SET mysql-max_connections=3000; SAVE MYSQL VARIABLES TO DISK; LOAD MYSQL VARIABLES TO RUNTIME\"", "approval_required": True},
    ],
)

# ── HAPROXY BACKEND DOWN ──────────────────────────────────────────────────
HAPROXY_CASES = [
    ("HAProxyBackendDown", "app-backend", "HAProxy backend app-backend has DOWN servers"),
    ("HAProxyBackendDown", "db-readonly", "HAProxy db-readonly pool: all replicas DOWN"),
    ("HAProxyAllBackendsDown", "api-upstream", "HAProxy api-upstream: ALL backends DOWN — service unavailable"),
]
for i, (alertname, backend, summary) in enumerate(HAPROXY_CASES, start=1):
    pid = f"sop-OS-HAPROXY-{i:04d}"
    add(
        pid, alertname, summary,
        [
            f"service_haproxy probe: result=FAILED down_backends=['{backend}/server1', '{backend}/server2']. "
            f"`echo 'show stat' | socat stdio /var/run/haproxy/stats` → {backend} BACKEND=DOWN servers=2/2 DOWN."
        ],
        f"HAProxy backend '{backend}' has servers in DOWN state. "
        "Health checks failing — upstream servers may be crashed, overloaded, or network-partitioned.",
        [
            {"layer": "L1", "command": "echo 'show stat' | socat stdio /var/run/haproxy/stats | cut -d',' -f1,2,18,19 | head -30", "rationale": "HAProxy backend/server status table"},
            {"layer": "L1", "command": f"echo 'show info' | socat stdio /var/run/haproxy/stats | grep -i error", "rationale": "HAProxy global error counters"},
        ],
        [
            {"step": f"Check upstream servers in backend '{backend}': ping, curl health endpoint, check service logs", "approval_required": False},
            {"step": "If server is healthy but HAProxy marks it DOWN: verify health-check URI and interval config", "approval_required": False},
        ],
    )

# ── OOM / KERNEL ──────────────────────────────────────────────────────────
OOM_CASES = [
    ("OOMKillOS", "multi-agent-app", "OS OOM killer invoked — process killed for memory",
     "OOM kill: process=multi-agent-app pid=28471 killed. dmesg: 'Out of memory: Kill process 28471'. Free memory=0 Swap=0.",
     "OS-level OOM killer triggered (not K8s OOMKilled). Host memory exhausted across all cgroups.",
     "Check total host memory vs pod/container limits; increase node memory or reduce workload density."),

    ("SwapExhausted", "all", "Host swap fully consumed — severe memory pressure",
     "remote_system_metrics: mem_pct=98.5 swap_pct=100. dmesg: 'kswapd: scan rate at maximum'. System thrashing.",
     "Host swap fully consumed. System is thrashing — excessive paging causing severe performance degradation.",
     "Immediately identify largest memory consumers and reduce or evict; adding swap is not sufficient at this stage."),

    ("KernelOOPS", "node-01", "Kernel OOPS / GPF — hardware or driver fault",
     "dmesg probe: 'general protection fault' 'kernel BUG at' 'unable to handle kernel paging request'. Machine may be in unstable state.",
     "Kernel OOPS detected. System integrity is uncertain. Live migration or reboot required for safety.",
     "Evacuate workloads, capture dmesg for RCA, schedule reboot during maintenance window."),
]
for i, (alertname, workload, summary, evidence_text, root_cause, fix) in enumerate(OOM_CASES, start=1):
    pid = f"sop-OS-OOM-{i:04d}"
    add(
        pid, alertname, summary,
        [evidence_text],
        root_cause,
        [
            {"layer": "L1", "command": "dmesg | grep -i 'oom\\|kill\\|out of memory' | tail -20", "rationale": "OOM kill events in kernel log"},
            {"layer": "L1", "command": "free -h && swapon --show", "rationale": "Current memory/swap state"},
            {"layer": "L1", "command": "ps aux --sort=-%mem | head -20", "rationale": "Top memory-consuming processes"},
        ],
        [{"step": fix, "approval_required": True}],
    )

# ── NETWORK OS-LEVEL ──────────────────────────────────────────────────────
NET_CASES = [
    ("NetworkInterfaceDown", "eth0", "Network interface eth0 link DOWN",
     "network probe: interface eth0 operstate=down carrier=0. `ip link show eth0` → state DOWN. All connectivity via this NIC lost.",
     "Physical or virtual NIC link is down. Cause: cable fault, switch port error, or VM NIC detached.",
     "Check physical/virtual switch port; `ip link set eth0 up`; if VM, verify NIC attachment in hypervisor."),

    ("DNSResolutionFailed", "all", "DNS resolution failing — NXDOMAIN / SERVFAIL",
     "network probe: dns_resolve_time=timeout SERVFAIL for internal domains. `/etc/resolv.conf` points to unreachable nameserver.",
     "DNS resolver unreachable or misconfigured. All name-resolution-dependent services failing.",
     "Check /etc/resolv.conf nameservers; verify DNS server health; fallback to known-good resolver temporarily."),

    ("SSLCertExpired", "api.internal", "TLS certificate expired — HTTPS handshake failing",
     "network probe: certificate expired 3 days ago. `openssl s_client -connect api.internal:443` → Verify return code: 1 (unable to verify the first certificate). curl → SSL_ERROR_EXPIRED.",
     "TLS certificate for api.internal expired. All HTTPS clients failing handshake.",
     "Renew certificate immediately; if Let's Encrypt: certbot renew --force-renewal; reload nginx/haproxy after."),
]
for i, (alertname, target, summary, evidence_text, root_cause, fix) in enumerate(NET_CASES, start=1):
    pid = f"sop-OS-NET-{i:04d}"
    add(
        pid, alertname, summary,
        [evidence_text],
        root_cause,
        [
            {"layer": "L1", "command": "ip link show && ip addr show", "rationale": "NIC state and IP assignment"},
            {"layer": "L1", "command": "ping -c 3 8.8.8.8 && dig google.com @8.8.8.8", "rationale": "Connectivity and DNS resolution test"},
            {"layer": "L1", "command": f"openssl s_client -connect {target}:443 -showcerts </dev/null 2>/dev/null | openssl x509 -noout -dates", "rationale": "Certificate validity window"},
        ],
        [{"step": fix, "approval_required": False}],
    )

# ── K8S HARD FAIL (OS-adjacent, not in existing dataset) ─────────────────
K8S_HARD_CASES = [
    ("NodeNotReadyOSLevel", "node-01", "Node NotReady due to kubelet or containerd crash",
     "k8s_hard_fail probe: node=node-01 status=NotReady condition=KubeletNotReady. "
     "`systemctl status kubelet` → failed. `journalctl -u kubelet -n 50` → 'failed to run kubelet: unable to load bootstrap kubeconfig'.",
     "Kubelet process crashed or lost kubeconfig. Node is partitioned from control plane — all pods on node unschedulable.",
     "Restart kubelet: systemctl restart kubelet; check kubeconfig exists and has valid credentials."),

    ("ContainerdCrash", "node-02", "containerd runtime crashed — pods unable to start",
     "k8s_hard_fail probe: node=node-02. `systemctl status containerd` → failed. "
     "New pod creates failing: 'connection error: unable to connect to CRI runtime'. Existing pods running from cached state.",
     "Container runtime (containerd) crashed. New pods cannot be created; existing pods continue until restart.",
     "systemctl restart containerd; if recurring, check /var/lib/containerd disk usage and /etc/containerd/config.toml."),

    ("NodeDiskPressure", "node-03", "Node DiskPressure condition — kubelet evicting pods",
     "k8s_hard_fail probe: node=node-03 DiskPressure=True. Kubelet evicting BestEffort pods. "
     "`df -h /var/lib/kubelet` → 97% full. imagefs also near capacity.",
     "Node disk pressure caused kubelet to set DiskPressure=True taint. BestEffort pods being evicted.",
     "Free disk: docker image prune -f; crictl rmi --prune; check large log files in /var/log/pods."),

    ("NodeMemoryPressure", "node-04", "Node MemoryPressure — kubelet evicting pods",
     "k8s_hard_fail probe: node=node-04 MemoryPressure=True available_memory=128Mi threshold=200Mi. "
     "Kubelet evicting pods. OOM killer active in kernel log.",
     "Node memory pressure: available memory below eviction threshold. Kubelet actively evicting pods.",
     "Identify memory-heavy pods: kubectl top pods --all-namespaces --sort-by=memory | head -20; consider node cordon + drain."),
]
for i, (alertname, node, summary, evidence_text, root_cause, fix) in enumerate(K8S_HARD_CASES, start=1):
    pid = f"sop-OS-K8S-{i:04d}"
    add(
        pid, alertname, summary,
        [evidence_text],
        root_cause,
        [
            {"layer": "L1", "command": f"systemctl status kubelet containerd", "rationale": "Node-level runtime services"},
            {"layer": "L1", "command": f"journalctl -u kubelet -n 50 --no-pager", "rationale": "Kubelet logs for error"},
            {"layer": "L3", "command": f"kubectl describe node {node} | grep -A20 'Conditions:'", "rationale": "Node condition detail from K8s API"},
        ],
        [{"step": fix, "approval_required": False}],
    )

# ── D1 PROCESS HEALTH ─────────────────────────────────────────────────────
CRON_CASES = [
    ("CronJobFailed", "backup-daily", "Daily backup cron job failed with exit code 1",
     "cron_jobs probe: failed_cron_count=1 failed_jobs=['backup-daily'] last_exit_code=1. "
     "`journalctl -u cron -n 50` shows 'backup-daily exited with status 1'. "
     "Disk usage at destination: 99% — not enough space.",
     "Cron job backup-daily failed due to no space on destination volume.",
     "df -h /backup; du -sh /backup/*; free space or move to another volume."),

    ("CronJobMissing", "cert-renewal", "Certificate renewal cron job stopped running",
     "cron_jobs probe: failed_cron_count=0 but last_run_timestamp is 72h ago (expected every 24h). "
     "cert-renewal job entry exists in crontab but not in journalctl recent logs.",
     "Cron job silently stopped executing — cron daemon may have restarted without reloading crontab, "
     "or job is locked by a stale PID file.",
     "Check cron daemon: systemctl status cron; verify crontab -l -u root; remove stale lockfile if present."),

    ("ZombieProcessAccumulation", "worker-pool", "Zombie process count exceeded threshold on node",
     "zombie_processes probe: zombie_count=47 parent_pid=1234 parent_cmd='worker-pool'. "
     "`ps aux | grep Z` shows 47 zombie child processes. Parent not reaping children (no SIGCHLD handler).",
     "Worker pool parent process not calling wait() on child processes. Zombies accumulate until parent exits.",
     "Send SIGHUP to parent: kill -HUP 1234; if process lacks proper SIGCHLD handler, restart the parent service."),

    ("OOMKillCritical", "java-app", "OOM killer terminated critical process",
     "oom_events probe: oom_count=3 recent_oom_victims=['java-app-prod','java-app-prod','java-app-worker']. "
     "`dmesg | grep -i 'oom kill'` → 'Out of memory: Kill process 9821 (java) score 712 or sacrifice child'. "
     "Available memory at kill time: 42MB / 32GB.",
     "JVM heap limit not set; java-app allocated heap continuously until OOM killer intervened.",
     "Set JVM heap: -Xmx8g -Xms2g; add memory limits in K8s PodSpec resources.limits.memory."),

    ("OOMKillKubelet", "kubelet", "OOM kill event affected kubelet process",
     "oom_events probe: oom_count=1 recent_oom_victims=['kubelet']. "
     "Node entered NotReady state 30s after OOM kill. kubelet restarted via systemd watchdog after 60s.",
     "Kubelet process killed by OOM killer — insufficient node memory margin. "
     "System-level pods not protected from eviction.",
     "Set kubelet as oom_score_adj=-999 via systemd override; ensure node allocatable memory reserves are set."),
]
for i, (alertname, workload, summary, evidence_text, root_cause, fix) in enumerate(CRON_CASES, start=1):
    pid = f"sop-OS-PROC-{i:04d}"
    add(
        pid, alertname, summary,
        [evidence_text],
        root_cause,
        [
            {"layer": "L1", "command": "ps aux | grep -E 'Z|defunct'", "rationale": "List zombie processes"},
            {"layer": "L1", "command": "dmesg -T | grep -i oom | tail -20", "rationale": "OOM kill events"},
            {"layer": "L1", "command": "journalctl -u cron -n 50 --no-pager", "rationale": "Cron job logs"},
        ],
        [{"step": fix, "approval_required": False}],
    )

# ── D2 STORAGE (RAID/LVM/SWAP) ────────────────────────────────────────────
STORAGE_CASES = [
    ("RAIDArrayDegraded", "md1", "RAID-5 array md1 degraded — one member disk failed",
     "raid_mdadm probe: degraded_arrays=['md1'] failed_devices=1 active_devices=4 total_devices=5. "
     "`cat /proc/mdstat` → 'md1 : active raid5 sdb[0] sdc[1] sdd[2] sde[3] [5/4] [UUUU_]'. "
     "sdX device showing SMART errors: Reallocated_Sector_Ct=4096.",
     "One RAID-5 disk member failed SMART checks and was removed from array. Array in degraded mode — no redundancy.",
     "Replace failed disk; add new disk: mdadm --add /dev/md1 /dev/sdX; monitor rebuild with watch cat /proc/mdstat."),

    ("RAIDArrayFailed", "md0", "RAID-1 root array md0 failed — both mirrors unreadable",
     "raid_mdadm probe: degraded_arrays=['md0'] failed_devices=2 array_state=inactive. "
     "`cat /proc/mdstat` → 'md0 : inactive sda[1](F) sdb[2](F)'. Both devices show I/O errors. "
     "Filesystem mounted read-only automatically.",
     "Both RAID-1 mirrors failed simultaneously — likely storage controller or backplane failure.",
     "Boot from rescue media; fsck on surviving disk; contact hardware vendor — likely controller failure."),

    ("LVMVolumeGroupPartial", "vg_data", "LVM volume group vg_data in partial mode",
     "lvm_volumes probe: partial_vgs=['vg_data'] failed_pvs=['/dev/sdc']. "
     "`vgs` → 'vg_data partial'. `pvs` → '/dev/sdc: MISSING'. "
     "Logical volumes on vg_data not mountable.",
     "Physical volume /dev/sdc missing from volume group. vg_data entered partial mode — data inaccessible.",
     "vgchange -ay --partial vg_data; investigate /dev/sdc with smartctl; restore from backup if disk failed."),

    ("SwapExhausted", "node-01", "Swap usage exceeds 90% — system memory critically low",
     "swap_usage probe: swap_used_pct=94 swap_used=15.3GB swap_total=16GB. "
     "`free -m` → Swap: 16384 total, 15470 used, 914 free. "
     "`vmstat 1 5` → si=1240 so=890 (heavy swap I/O).",
     "System swapping heavily — insufficient RAM for workload. Performance severely degraded.",
     "Identify top memory consumers: ps aux --sort=-%mem | head -20; kill non-critical processes; "
     "add RAM or reduce pod memory limits."),

    ("SwapDisabled", "node-02", "Swap disabled but OOM events occurring",
     "swap_usage probe: result=WARN swap_total=0 oom_events=5_last_hour. "
     "K8s cluster requires swap disabled; workloads exceed node RAM. "
     "`free -m` → Swap: 0 total, 0 used.",
     "No swap configured (correct for K8s) but workloads exceeding node RAM capacity without OOM protection.",
     "Add memory resource limits to all pods; enable swap on non-K8s nodes or add node capacity."),
]
for i, (alertname, vol, summary, evidence_text, root_cause, fix) in enumerate(STORAGE_CASES, start=1):
    pid = f"sop-OS-STOR-{i:04d}"
    add(
        pid, alertname, summary,
        [evidence_text],
        root_cause,
        [
            {"layer": "L1", "command": "cat /proc/mdstat", "rationale": "RAID array status"},
            {"layer": "L1", "command": "pvs; vgs; lvs", "rationale": "LVM physical/volume/logical status"},
            {"layer": "L1", "command": "free -m; vmstat 1 3", "rationale": "Memory and swap utilization"},
        ],
        [{"step": fix, "approval_required": True}],
    )

# ── D3 NETWORK (additional) ───────────────────────────────────────────────
NETWORK_CASES = [
    ("NetworkInterfaceDown", "eth1", "Physical network interface eth1 down",
     "network_interfaces probe: down_interfaces=['eth1'] error_interfaces=[]. "
     "`ip link show eth1` → 'eth1: <BROADCAST,MULTICAST> mtu 1500 qdisc noop state DOWN'. "
     "ethtool eth1 → 'Link detected: no'.",
     "Physical NIC eth1 lost link — cable disconnected, switch port down, or NIC hardware failure.",
     "Check cable: ethtool eth1; test switch port; ip link set eth1 up; if persistent, replace NIC or cable."),

    ("NetworkInterfaceErrors", "bond0", "Bond interface reporting high TX/RX errors",
     "network_interfaces probe: error_interfaces=['bond0'] tx_errors=12400 rx_errors=8900. "
     "`ip -s link show bond0` → RX errors 8900, TX errors 12400. "
     "One bond member (eth2) showing CRC errors via ethtool.",
     "Bond member eth2 experiencing hardware errors — faulty cable or NIC causing frame corruption.",
     "Identify bad member: ethtool eth2 | grep errors; ip link set eth2 nomaster; replace cable or NIC."),

    ("DNSResolutionFailing", "coredns", "DNS resolution failing for all queries from pods",
     "dns_resolution probe: failed_lookups=['kubernetes.default.svc.cluster.local','google.com'] "
     "lookup_error_count=847. "
     "`dig +short kubernetes.default @10.96.0.10` → ';; connection timed out'. "
     "CoreDNS pods Running but not responding.",
     "CoreDNS pods Running but TCP connections not reaching them — likely iptables/kube-proxy rules stale.",
     "Restart kube-proxy: kubectl rollout restart ds/kube-proxy -n kube-system; "
     "then restart CoreDNS: kubectl rollout restart deploy/coredns -n kube-system."),

    ("DNSNXDOMAINFlood", "dns-resolver", "DNS NXDOMAIN rate exceeds threshold — possible misconfiguration",
     "dns_resolution probe: nxdomain_rate=1240_per_min lookup_error_count=0 nxdomain_domains=['db.legacy.svc']. "
     "Applications querying removed service 'db.legacy.svc'. "
     "CoreDNS logs: NXDOMAIN flood from namespace 'app-production'.",
     "Application pods configured with stale DNS name 'db.legacy.svc' that no longer exists. "
     "NXDOMAIN responses causing connection retry storms.",
     "Update app config: helm upgrade with correct service DNS name; "
     "CoreDNS rewrite plugin can stub the old name temporarily."),

    ("TCPTimeWaitAccumulation", "node-03", "TCP TIME_WAIT connections exceeding 50000",
     "tcp_connections probe: time_wait_excess=True time_wait_count=62400 established=8900. "
     "`ss -s` → TIME-WAIT: 62400. `netstat -an | awk '{print $6}' | sort | uniq -c` confirms. "
     "Port exhaustion imminent: available ephemeral ports = 65535 - 62400 = 3135.",
     "Short-lived HTTP connections not reusing TCP sessions. tw_reuse disabled. "
     "Port exhaustion causes new connection failures.",
     "Enable TCP reuse: sysctl -w net.ipv4.tcp_tw_reuse=1; "
     "enable keepalive: sysctl -w net.ipv4.tcp_keepalive_time=60; "
     "implement HTTP keep-alive in application."),

    ("SYNFloodDetected", "node-04", "SYN flood attack detected on port 443",
     "tcp_connections probe: syn_flood_indicator=True syn_backlog_overflow=True port=443. "
     "`netstat -an | grep SYN_RECV | wc -l` → 4800. "
     "kernel: TCP: request_sock_TCP: Possible SYN flooding on port 443. Sending cookies.",
     "SYN flood attack on port 443. SYN cookies enabled but backlog exhausted.",
     "Enable SYN cookies: sysctl -w net.ipv4.tcp_syncookies=1; "
     "increase backlog: sysctl -w net.ipv4.tcp_max_syn_backlog=4096; "
     "add firewall rate-limit: iptables -A INPUT -p tcp --syn -m limit --limit 1/s -j ACCEPT."),
]
for i, (alertname, iface, summary, evidence_text, root_cause, fix) in enumerate(NETWORK_CASES, start=1):
    pid = f"sop-OS-NET-{i:04d}"
    add(
        pid, alertname, summary,
        [evidence_text],
        root_cause,
        [
            {"layer": "L1", "command": "ip link show; ip -s link", "rationale": "Interface state and error counters"},
            {"layer": "L1", "command": "ss -s; ss -tn state time-wait | wc -l", "rationale": "TCP connection summary"},
            {"layer": "L1", "command": "dig +short kubernetes.default.svc.cluster.local @$(kubectl get svc -n kube-system kube-dns -o jsonpath='{.spec.clusterIP}')", "rationale": "DNS resolution test"},
        ],
        [{"step": fix, "approval_required": False}],
    )

# ── D4 DATABASE (additional) ──────────────────────────────────────────────
DB_CASES = [
    ("PostgreSQLReplicationLag", "pg-replica-01", "PostgreSQL replica lag exceeds 30s",
     "postgresql_health probe: replication_lag_s=147 result=WARN replica_state=streaming. "
     "`SELECT * FROM pg_stat_replication` → replay_lag=00:02:27. "
     "Primary write rate: 45MB/s; replica network: 1Gbps saturated.",
     "Replica replication lag caused by network saturation. WAL shipping rate cannot keep up with write load.",
     "Check wal_keep_size; increase network bandwidth or switch to logical replication with filtering; "
     "consider pglogical for selective replication."),

    ("RedisOSMemoryLimit", "redis-01", "Redis instance using 95% of available OS memory",
     "redis_os_health probe: result=WARN used_memory_human=14.8GB maxmemory=15GB eviction_policy=noeviction. "
     "`redis-cli INFO memory` → used_memory_peak=15845123328 maxmemory=16106127360. "
     "Eviction policy=noeviction → new writes will fail when limit reached.",
     "Redis approaching maxmemory limit with noeviction policy. New SET operations will return OOM error.",
     "Change eviction policy: redis-cli CONFIG SET maxmemory-policy allkeys-lru; "
     "or increase maxmemory: redis-cli CONFIG SET maxmemory 24gb."),

    ("MongoDBReplicaStale", "mongo-rs1-secondary", "MongoDB replica set secondary is 120s behind primary",
     "mongodb_health probe: repl_lag_s=120 result=WARN rs_state=SECONDARY oplog_lag=PT2M. "
     "`db.adminCommand('replSetGetStatus')` → members[1].optimeDate is 2 minutes behind primary. "
     "oplog window: 24h — not at risk of falling off.",
     "MongoDB secondary replication lag 120s — likely caused by heavy writes to primary exceeding secondary apply throughput.",
     "Check secondary resources: db.serverStatus().opcounters; if CPU-bound, scale secondary instance; "
     "review write concern w:majority impact."),
]
for i, (alertname, db_host, summary, evidence_text, root_cause, fix) in enumerate(DB_CASES, start=1):
    pid = f"sop-OS-DB2-{i:04d}"
    add(
        pid, alertname, summary,
        [evidence_text],
        root_cause,
        [
            {"layer": "L1", "command": "redis-cli INFO replication; redis-cli INFO memory", "rationale": "Redis replication and memory state"},
            {"layer": "L1", "command": "psql -c 'SELECT * FROM pg_stat_replication'", "rationale": "PostgreSQL replica state"},
            {"layer": "L1", "command": "mongo --eval 'db.adminCommand(\"replSetGetStatus\")'", "rationale": "MongoDB replica set status"},
        ],
        [{"step": fix, "approval_required": False}],
    )

# ── D6 HARDWARE ERRORS ────────────────────────────────────────────────────
HARDWARE_CASES = [
    ("KernelMCEError", "node-05", "Machine Check Exception (MCE) detected — hardware error",
     "kernel_errors probe: critical_errors=['MCE: PROCESSOR 0:906EA BANK 5'] mce_count=12. "
     "`dmesg -T | grep -i mce` → 'mce: [Hardware Error]: Machine check events logged'. "
     "`mcelog --client` → 'MEMORY CONTROLLER RD_CHANNEL0_ERR'. Memory controller error bank 5.",
     "Hardware MCE on memory controller channel 0. DIMM in slot A1 likely failing. "
     "Correctable ECC errors accumulating — may progress to uncorrectable.",
     "Schedule node drain: kubectl drain node-05 --ignore-daemonsets; "
     "replace DIMM in slot A1; run memtest86+ before returning to service."),

    ("KernelIOError", "node-06", "Kernel I/O errors on storage device /dev/sdb",
     "kernel_errors probe: critical_errors=['blk_update_request: I/O error, dev sdb'] mce_count=0. "
     "`dmesg -T | grep sdb` → 'end_request: I/O error, dev sdb, sector 4096000'. "
     "SMART: Reallocated_Sector_Ct=8192 Offline_Uncorrectable=47.",
     "SATA disk /dev/sdb experiencing hardware failures. 47 offline uncorrectable sectors. "
     "Data loss risk if disk not replaced immediately.",
     "Set disk offline: echo offline > /sys/block/sdb/device/state; "
     "if RAID member: mdadm --fail /dev/mdX /dev/sdb; schedule disk replacement."),

    ("MemoryECCCorrectableHigh", "node-07", "ECC correctable memory errors exceeding threshold",
     "memory_hw_errors probe: correctable_errors=2847 uncorrectable_errors=0 dimm_slot='A2'. "
     "`edac-util -s` → 'mc0: 2847 Corrected Errors'. "
     "`ipmitool sel list` → 'Memory ECC correctable error DIMM_A2 count=2847'.",
     "DIMM A2 experiencing high correctable ECC error rate — hardware degradation, "
     "not yet causing data corruption but likely to progress to uncorrectable.",
     "Drain and schedule maintenance: kubectl drain node-07; replace DIMM A2 slot; "
     "monitor post-replacement: edac-util -s for 24h."),

    ("MemoryECCUncorrectable", "node-08", "ECC uncorrectable memory error — system panic imminent",
     "memory_hw_errors probe: correctable_errors=0 uncorrectable_errors=3 result=FAILED. "
     "`dmesg -T | grep -i 'uncorrectable'` → 'EDAC MC0: 3 UE errors on mc#0csrow#1channel#0'. "
     "System has already panic-reset twice in last 4 hours.",
     "Uncorrectable ECC errors — DIMM hardware failure causing data corruption. "
     "System panic loop will continue until DIMM replaced.",
     "Immediate action: kubectl drain --force node-08; power off node; replace DIMM; "
     "do NOT restart without hardware fix — data corruption risk."),

    ("DiskControllerError", "node-09", "RAID controller reporting critical error",
     "kernel_errors probe: critical_errors=['megaraid_sas: FAILED to init firmware'] mce_count=0. "
     "`dmesg | grep megaraid` → 'megaraid_sas 0000:03:00.0: FW fault state detected'. "
     "All RAID volumes offline. Boot to BIOS reports controller in fault state.",
     "MegaRAID controller firmware fault. All volumes hosted on this controller are offline.",
     "Boot from recovery media; check controller battery backup; "
     "try firmware reset via CTRL+R at boot; contact vendor support — possible controller hardware failure."),
]
for i, (alertname, node, summary, evidence_text, root_cause, fix) in enumerate(HARDWARE_CASES, start=1):
    pid = f"sop-OS-HW-{i:04d}"
    add(
        pid, alertname, summary,
        [evidence_text],
        root_cause,
        [
            {"layer": "L1", "command": "dmesg -T | grep -E 'mce|MCE|error|Error' | tail -30", "rationale": "Recent kernel hardware errors"},
            {"layer": "L1", "command": "edac-util -s 2>/dev/null || ipmitool sel list | tail -20", "rationale": "ECC error counters via EDAC or IPMI"},
            {"layer": "L1", "command": "smartctl -H /dev/sda; smartctl -H /dev/sdb", "rationale": "SMART health for attached disks"},
        ],
        [{"step": fix, "approval_required": True}],
    )

# ── D7 CONTAINER RUNTIME ──────────────────────────────────────────────────
CONTAINER_CASES = [
    ("DockerDaemonCrash", "node-10", "Docker daemon crashed — container operations failing",
     "docker_daemon probe: daemon_error='connection refused' unhealthy_containers=0. "
     "`systemctl status docker` → 'Active: failed (Result: exit-code)'. "
     "`journalctl -u docker -n 30` → 'failed to start daemon: error initializing graphdriver: "
     "overlay2: failed to Mount'.  /var/lib/docker filesystem 100% full.",
     "Docker daemon failed to start because overlay2 storage driver mount failed — disk full.",
     "Free disk space: docker system prune -f; journalctl --vacuum-size=1G; "
     "then: systemctl start docker."),

    ("DockerUnhealthyContainers", "node-11", "Multiple Docker containers in unhealthy state",
     "docker_daemon probe: daemon_error=None unhealthy_containers=8. "
     "`docker ps --filter health=unhealthy` → 8 containers in unhealthy state. "
     "HEALTHCHECK commands timing out: 'health: maximum consecutive failures exceeded'.",
     "8 containers failing their configured HEALTHCHECK probes. "
     "Likely backend services they depend on are down (database, cache).",
     "docker inspect <container_id> | jq '.[0].State.Health'; "
     "identify failing healthcheck command; fix dependency or tune healthcheck timeout."),

    ("ContainerdCrashLoop", "node-12", "containerd entering crash loop — pod creates failing",
     "containerd_state probe: daemon_error='rpc error: code=Unavailable' plugin_errors=['io.containerd.snapshotter.v1.overlayfs']. "
     "`systemctl status containerd` → 'activating (start) / start-pre'. "
     "overlayfs snapshotter plugin failing to initialize — /var/lib/containerd 98% full.",
     "containerd snapshotter plugin failing because /var/lib/containerd storage is nearly full. "
     "New image pulls and container creates fail.",
     "Free space: crictl rmi --prune; find /var/lib/containerd -name '*.img' -mtime +7 -delete; "
     "systemctl restart containerd."),

    ("ContainerdPluginError", "node-13", "containerd CNI plugin error — pod networking broken",
     "containerd_state probe: daemon_error=None plugin_errors=['cni-bridge: failed to set bridge addr']. "
     "`journalctl -u containerd -n 30` → 'CNI failed to setup network for sandbox: "
     "failed to set bridge addr: bridge with same IP already exists'. "
     "New pods stuck in ContainerCreating.",
     "CNI bridge plugin conflict — existing bridge interface has same IP as new pod CIDR assignment. "
     "Stale CNI state from previous pod network teardown.",
     "Clean stale CNI state: rm -rf /var/lib/cni/networks/bridge/*; "
     "ip link delete cni0 2>/dev/null; ip link delete flannel.1 2>/dev/null; "
     "systemctl restart containerd kubelet."),

    ("DockerDiskQuotaExceeded", "node-14", "Docker container storage quota exceeded",
     "docker_daemon probe: daemon_error=None unhealthy_containers=3 disk_usage_pct=97. "
     "`docker system df` → Images: 45.2GB, Containers: 12.1GB, Local Volumes: 8.9GB, Build Cache: 22.4GB. "
     "New container creates failing: 'no space left on device'.",
     "Docker build cache and stopped container layers consuming 88GB+. No automatic cleanup configured.",
     "docker system prune --volumes -f; "
     "add cronjob: 0 3 * * * /usr/bin/docker system prune -f --filter 'until=168h'; "
     "set daemon.json: {\"log-opts\":{\"max-size\":\"10m\",\"max-file\":\"3\"}}."),
]
for i, (alertname, node, summary, evidence_text, root_cause, fix) in enumerate(CONTAINER_CASES, start=1):
    pid = f"sop-OS-CTR-{i:04d}"
    add(
        pid, alertname, summary,
        [evidence_text],
        root_cause,
        [
            {"layer": "L1", "command": "systemctl status docker containerd", "rationale": "Container runtime service state"},
            {"layer": "L1", "command": "docker system df; docker ps --filter health=unhealthy", "rationale": "Docker disk usage and unhealthy containers"},
            {"layer": "L1", "command": "ctr version; ctr plugins ls 2>/dev/null", "rationale": "containerd plugin state"},
        ],
        [{"step": fix, "approval_required": False}],
    )

# ── D5 PROXY/LB (additional) ──────────────────────────────────────────────
PROXY_CASES = [
    ("NginxUpstreamDown", "nginx-prod", "Nginx upstream group fully down — all backends offline",
     "service_nginx probe: error_rate_pct=100 upstream_errors=['upstream timed out (110)','no live upstreams']. "
     "`nginx -t` → 'syntax is ok'. `curl -I localhost` → 502 Bad Gateway. "
     "upstream group 'app_backend' has 0 of 5 servers responding.",
     "All upstream application servers in app_backend group are offline or not responding. "
     "Nginx returning 502 to all requests.",
     "Check upstream health: curl http://app-server-01:8080/health; "
     "kubectl get pods -l app=backend -n production; "
     "if pods Running, check service selector and endpoint: kubectl get endpoints backend-svc."),

    ("NginxConfigError", "nginx-prod", "Nginx configuration reload failed after ConfigMap change",
     "service_nginx probe: result=FAILED daemon_error='nginx: configuration file test failed'. "
     "`nginx -t` → 'nginx: [emerg] unknown directive \"proxy_set_headerr\" in /etc/nginx/conf.d/app.conf:14'. "
     "Typo in directive name introduced by recent ConfigMap update.",
     "Nginx configuration contains typo 'proxy_set_headerr'. Config test failed; "
     "nginx reload rejected — currently serving with previous (good) config.",
     "Fix ConfigMap typo; apply: kubectl apply -f nginx-cm.yaml; "
     "exec into pod: nginx -t; if OK: nginx -s reload."),

    ("KeepalivedFailover", "vip-01", "VRRP failover occurred — VIP moved to backup node",
     "service_keepalived probe: result=WARN state=BACKUP vip_assigned_to='node-02' was_master=True. "
     "`journalctl -u keepalived` → 'Transition to BACKUP state'. "
     "node-01 network interface flap detected 120s before state change.",
     "Keepalived master node-01 lost VRRP advertisement due to network interface flap. "
     "node-02 (backup) correctly assumed MASTER role. VIP functional on node-02.",
     "Verify VIP: ip addr show | grep <VIP>; ping <VIP>; "
     "investigate node-01 NIC: ethtool eth0 | grep 'Link detected'; "
     "if stable, manually preempt back: systemctl restart keepalived on node-01."),
]
for i, (alertname, svc, summary, evidence_text, root_cause, fix) in enumerate(PROXY_CASES, start=1):
    pid = f"sop-OS-PROXY-{i:04d}"
    add(
        pid, alertname, summary,
        [evidence_text],
        root_cause,
        [
            {"layer": "L1", "command": "nginx -t; nginx -s reload 2>&1 | head -5", "rationale": "Nginx config validity"},
            {"layer": "L1", "command": "ip addr show | grep -A2 'inet.*secondary'", "rationale": "VIP assignment on this node"},
            {"layer": "L1", "command": "journalctl -u keepalived -n 30 --no-pager", "rationale": "VRRP state transitions"},
        ],
        [{"step": fix, "approval_required": False}],
    )

# ── Write output ──────────────────────────────────────────────────────────
OUT.parent.mkdir(parents=True, exist_ok=True)
with open(OUT, "w") as f:
    for r in RECORDS:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

print(f"Written {len(RECORDS)} records to {OUT}")
