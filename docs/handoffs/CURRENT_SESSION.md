# Current Session Handoff

## Deliverable hiện tại
**Living Operations Runtime — Step 3: Durable Command Delivery + Acknowledgement + Agent Resume.**
Backend-first (governing correction 2026-07-01): backend/runtime là sản phẩm chính; portal =
projection. Slice này fix P0 (GET=POP) và thêm giao lệnh MUTATING durable end-to-end.

## Trạng thái hiện tại — HOÀN TẤT (unit/integration) + proof harness cho hạ tầng thật

### Đã commit
- `6d3c429` feat(aoip): Provider/Tenant portal projection + governance correction (checkpoint riêng).

### Chưa commit (slice Step 3, turn này)
- **Gateway** `src/gateway/routes/agent_runtime.py` — durable route `/webhook/agent/rt`
  (peek + enqueue/accept/progress/terminal/record). Đăng ký ở `src/gateway/api.py`.
  Self-contained, KHÔNG import aoip (Dockerfile.gateway không COPY src/aoip).
- **Agent core** `src/aoip/agent/`:
  - `delivery.py` — `DurableCommandChannel` (twin phía AOIP; máy trạng thái QUEUED→…→terminal).
  - `inbox.py` — `LocalInbox` durable (fsync+atomic), local lifecycle + resume flags.
  - `delivery_loop.py` — `DeliveryLoop.resume()/tick()` (persist trước execute, re-report,
    reconcile RUNNING, never blind retry).
  - `daemon.py` — `run_daemon()` vòng bền (register→heartbeat→resume→tick→sleep); executor
    mặc định no-op an toàn (nối recovery mutation là follow-up).
  - `omni_client.py` — HTTPOmniClient +poll_runtime/accept/progress/report_terminal.
- **systemd** `deploy/systemd/aoip-agent.service` (StateDirectory giữ inbox qua reboot).
- **Proof** `scripts/prove_durable_delivery.py` (Gateway/Redis K8s + VM systemd, 8 case DoD).
- **Tests (25, all green)**: `tests/test_aoip_delivery.py` (16), `test_aoip_delivery_loop.py` (4),
  `test_aoip_agent_daemon.py` (1), `test_gateway_agent_runtime.py` (4 qua ASGI thật).
- **Docs**: CHANGELOG [Unreleased], `docs/plans/living-operations-hardening.md` Bước 3 ✅.

## Inspect kết luận (Step 1)
- Command lưu: Redis LIST `omni:agent:cmd:{agent_id}` (kênh cũ). GET = **RPOP (pop-on-read)**.
- cmd_id: `cmd-{uuid4[:12]}` sinh lúc enqueue. Delivery KHÔNG durable, KHÔNG redelivery/ack.
- Kết quả terminal: `omni:diag:cmdresult:{cmd_id}` STRING TTL 3600.
- **P0**: kênh cũ pop-on-read — NHƯNG chỉ phục vụ command chẩn đoán READ-ONLY (không mutation).
  Command MUTATING trước đây KHÔNG có kênh durable → thêm surface mới `/rt` thay vì sửa kênh cũ
  (tránh vỡ hợp đồng remote-agent diagnostic hiện có).

## Verification đã chạy
- `.venv/bin/python -m pytest tests/ -q -k "aoip or gateway_agent"` → **222 passed**.
- `PYTHONPATH=src python -c "import gateway.api"` → OK (router mới nạp sạch).

## Next step chính xác
1. Nối `executor` của daemon vào recovery mutation thật (`operations.py`/`recovery.py` +
   IdempotencyLedger) — hiện no-op; đây là điểm mutation duy nhất còn để nối.
2. Chạy `scripts/prove_durable_delivery.py` trên Gateway K8s + 3 VM systemd (case 2/3/5 + reboot).
3. Portal projection: hiện thực stub nav (Agents/Incidents) đọc `/webhook/agent/rt/commands/record`
   — chỉ khi có nhu cầu; KHÔNG thêm UI field không có nguồn backend.

## Không được làm lại
- KHÔNG đổi kênh cũ `/commands/{agent_id}` sang peek (nó là read-only diagnostic, hợp đồng khác).
- KHÔNG import aoip vào gateway route (deploy boundary). Twin duy trì contract HTTP, không key Redis.
- KHÔNG archive local inbox trước terminal ack. Dedup post-archive thuộc Gateway terminal record + ledger.
- KHÔNG frontend-led product domain; KHÔNG mock/fixture metric.

## Branch
`feature/living-operations-runtime`, HEAD `6d3c429`. Slice Step 3 CHƯA commit (chờ người dùng).
