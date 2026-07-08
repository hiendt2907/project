# IT-4 — Parity checklist: `remote_agent` vs `aoip.agent.daemon`

> Sprint "Nhân viên SRE" (`sprint-agent-sre-employee-production.md`), IT-4 bước 1.
> Lập TRƯỚC khi chạm VM (bắt buộc theo risk register). Ngày: 2026-07-08.
> Mỗi capability có disposition: **PORT** (đưa vào aoip runtime) hoặc **ACCEPT-GAP**
> (chấp nhận thiếu tạm, ghi rõ iteration nào đóng).

## Nguyên tắc port (theo ADR-001)

- Runtime canonical = `aoip.agent.daemon`; **entrypoint mới nằm ở `src/aoip/agent/`**.
- `src/remote_agent/` KHÔNG nhận feature mới, nhưng **được reuse như library**:
  collectors/discovery/emitter/command_executor là code đã battle-tested trên 3 VM
  (~1.400 dòng) — copy sang aoip là vi phạm DRY và tự tạo drift risk. Import ≠ mở rộng.
- Chiến lược pilot: **một process, hai vòng song song** — `asyncio.gather(`
  telemetry loop (reuse `remote_agent.agent.run_agent()` nguyên vẹn) `,`
  durable command loop (`aoip.agent.daemon.run_daemon()`) `)`.
  Collector behavior giữ nguyên 100% → Twin fact parity được bảo đảm bằng cấu trúc,
  không phải bằng hy vọng.
- Pilot chạy `AOIP_AGENT_MODE=observe_only` (mặc định fail-safe). Kill-switch
  `OMNI_AUTO_EXECUTE_ENABLED=false` giữ nguyên toàn sprint.

## Bảng parity

| # | Capability (production trên 3 VM) | Nguồn | aoip daemon hiện có? | Disposition |
|---|---|---|---|---|
| 1 | Lane 1 system metrics + thresholds push từ Omni | `collectors/system.py`, `agent.py:177` | ❌ | **PORT** (reuse qua telemetry loop) |
| 2 | Lane 2 log errors | `collectors/logs.py` | ❌ | **PORT** (reuse) |
| 3 | Lane 3 K8s status (opt) | `collectors/k8s.py` | ❌ | **PORT** (reuse; cust-app không bật) |
| 4 | Lane 4 MySQL/ProxySQL health (opt) | `collectors/database.py` | ❌ | **PORT** (reuse; cust-app không bật) |
| 5 | Lane 5 HAProxy/systemd units (opt) | `collectors/services.py` | ❌ | **PORT** (reuse) |
| 6 | Lane 6 disk/NFS (opt) | `collectors/storage.py` | ❌ | **PORT** (reuse) |
| 7 | Lane 7 discovery evidence — 5 probes, gồm doc snapshot hash-tại-nguồn (IT-1) | `collectors/discovery_evidence.py` | ❌ | **PORT** (reuse — Twin fact của cust-app phụ thuộc trực tiếp; xem Productization Iteration 2) |
| 8 | VM auto-discovery startup + re-scan 1h + `derive_enabled_collectors` + upload profile | `discovery.py`, `agent.py:58-97,166` | ❌ | **PORT** (reuse) |
| 9 | Register keepalive 30s: capabilities, version, k8s_namespace, tenant_id, **local_ip** (NAT topology fix), **bundle_sha256** (IT-2), header `X-Omni-Agent-Id` | `emitter.py:82-113` | ⚠️ Partial — `HTTPOmniClient.register` thiếu local_ip/bundle_sha256/k8s_namespace/header; bỏ luôn response (không nhận thresholds) | **PORT** (telemetry loop dùng `OmniEmitter` sẵn có → tự đủ) |
| 10 | Evidence emit batch + retry 1s/2s/4s | `emitter.py:57-66,115-129` | ⚠️ Partial — `submit_evidence` có nhưng không retry | **PORT** (dùng `OmniEmitter.emit`) |
| 11 | Legacy diagnostic command channel: poll 5s + whitelist read-only executor (INV_READONLY_CMDS / INV_NO_DATA_EXFIL) + submit result đầy đủ (blocked/stderr/duration_ms) | `command_executor.py`, `agent.py:248-268` | ⚠️ Partial — `fetch_missions` đọc `/commands` nhưng map thành goal, không exec whitelist, `submit_result` thiếu field | **PORT** (reuse `execute_batch` qua telemetry loop) |
| 12 | UPDATE_AGENT + updater (download/sha256/restart) | `updater.py`, `command_executor.py:180` | ❌ | **ACCEPT-GAP** — domain IT-5 (safe update/rollback qua command channel làm ở IT-5; pilot cập nhật thủ công qua orb) |
| 13 | Bundle self-hash drift detection (IT-2) | `bundle_hash.py` | ❌ — hash hiện chỉ cover `src/remote_agent`; VM chạy AOIP sẽ ship thêm package `aoip` mà manifest không biết | **PORT + mở rộng**: agent AOIP báo thêm `aoip_bundle_sha256`; manifest release publish cả hai hash; agent cũ (2 VM còn lại) chỉ báo hash cũ → backward-compat, không bị đánh drifted oan |
| 14 | Enrollment + per-agent credential (IT-3) | `aoip/agent/enrollment.py` (canonical đã ở aoip) | ✅ | — (cust-app đã chạy per-agent key) |
| 15 | Cấu hình qua env / run.env (`AgentSettings`) | `settings.py` | ❌ — `daemon.main()` chỉ nhận argparse | **PORT** — entrypoint mới đọc cùng bộ env `OMNI_AGENT_*` (run.env hiện hữu trên VM dùng lại nguyên vẹn) |
| 16 | Durable mutating command channel `/rt` (inbox fsync, resume sau reboot, lease, fencing, idempotency) | — | ✅ aoip-only (`delivery_loop.py`, `inbox.py`, `intake.py`) | Lợi ích chính của migration — bật trong pilot ở observe_only |
| 17 | Runtime mode fail-closed (observe_only / mutation_enabled) | — | ✅ aoip-only (`runtime_config.py`) | Pilot: observe_only |

## Gap chấp nhận tạm (ghi danh chính thức)

1. **#12 UPDATE_AGENT**: AOIP daemon pilot KHÔNG tự update qua command channel — IT-5 đóng.
   Trong IT-4 mọi thay đổi code trên cust-app làm thủ công qua `orb -m cust-app` + rollback
   bằng systemd unit cũ.
2. **`aoip.agent.main` (mission understand_host)**: KHÔNG dùng cho pilot — entrypoint pilot là
   daemon + telemetry loop. Mission-driven observe là IT-8 (stretch).

## Việc kỹ thuật rút ra cho bước port (task #2)

1. `src/aoip/agent/employee.py` (entrypoint mới): đọc env `OMNI_AGENT_*` + `AOIP_AGENT_MODE`,
   chạy `asyncio.gather(run_agent(), run_daemon(...))`, SIGTERM dừng cả hai.
2. `bundle_hash`: thêm hash cho package `aoip` (subset ship lên VM); mở rộng
   `scripts/publish_agent_release.py` → manifest `{bundle_sha256, aoip_bundle_sha256}`;
   gateway `/webhook/agent/versions` so sánh field nào agent có báo.
3. `scripts/omni-agent-bundle.sh`: rsync thêm `src/aoip` (closure 27 module cho daemon;
   đơn giản nhất: ship cả package `aoip`, exclude `console/`, `__pycache__`).
4. Systemd unit mới `aoip-agent.service`: `ExecStart=... -m aoip.agent.employee`,
   `StateDirectory` cho inbox durable `/var/lib/aoip/inbox`, EnvironmentFile = run.env hiện có.
5. External deps trên VM: `httpx` (đã có trong requirements-agent.txt); `redis` client KHÔNG
   cần cho observe_only (import lazy trong `runtime_config.py`).

## Điều kiện DoD IT-4 (từ plan, nhắc lại)

`cust-app` chạy AOIP daemon ≥24h · Twin không mất fact so baseline · parity report vào
`PRODUCT_PROOF.md` · rollback diễn tập thật 1 lần (switch về `omni-remote-agent.service`).
