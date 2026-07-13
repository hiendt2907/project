# Current Session Handoff

## Deliverable hiện tại
**IT-5 (safe update/rollback qua durable command channel) — DONE toàn phần (2026-07-13):
code + 21 test mới + deploy gateway + drill (a)(b) trên VM thật + migrate fleet 3/3 employee
1.3.0 current.** PRODUCT_PROOF Iteration 31 đã ghi đầy đủ. Cùng phiên: portal đợt 1 +
fix /pipeline (commit `c27c86a`, `dd9f9c9`) và IT-4 đóng (`fe4d7a7`).

## IT-5 — bằng chứng chính (đừng re-verify, xem PRODUCT_PROOF Iteration 31)
- Drill (a): cust-app tự update 1.2.0→1.3.0 qua enqueue `/rt/commands/enqueue`, record
  `omni:cmd:rec:staging-sim:upd-1-3-0-drill-a` = COMPLETED/updated, delivery_count=1.
- Drill (b): expected-hash sai → health-gate fail → restore N-1 → record
  `upd-broken-drill-b` = FAILED/rolled_back restored=True, VM về bản lành.
- cust-edge/cust-db: enable aoip-agent (employee), rồi update lên 1.3.0 BẰNG chính cơ chế
  → `/versions` 3/3 current drifted=0. Unit cũ omni-remote-agent giữ disabled (rollback path).
- Cơ chế: `src/aoip/agent/updater.py` (executor verb UPDATE_AGENT + startup_gate +
  reconciler + guard shell `scripts/aoip-agent-guard.sh` ExecStartPre). Bundle tải từ gateway
  `/webhook/agent/release/bundle` (Redis base64, publish qua `make publish-agent-release`).
- VERSION repo đã bump 1.3.0.

## Known behavior / quan sát treo
- Update restart: systemd phải KILL sau TimeoutStopSec=30 (executor block-forever chủ ý) —
  +30s mỗi update, vô hại. Có thể tối ưu sau (exit chủ động sau khi schedule restart).
- Registry `tenant-replay-01` (2 record 1.1.3 unknown) vẫn "online" — cần truy nguồn
  heartbeat tenant replay ở iteration sau, KHÔNG chặn sprint.

## Trạng thái sprint NV-SRE
IT-1..IT-5 DONE. Còn: **IT-6** (command outcome durability PG + chaos proof: kill agent giữa
RUNNING + restart gateway → đúng 1 outcome trong PG) → **IT-7** (soak + offline recovery:
reboot/cắt mạng 10p từng VM, chạy lại e2e_onboarding_full_flow 10 TC, điền cột SAU 6 metric
sprint, cập nhật ADR-001) → IT-8 stretch (mission skeleton).

## Portal (đợt 1 DONE — c27c86a + dd9f9c9, 18/18 E2E)
Gap đợt 2 (user muốn "mọi backend hiển thị, non-tech hiểu"): incident drill-down (console
`/incident/{tenant}/{cid}` có sẵn), advisory/brain card VI, support-access, workers health,
KB stats, Việt hoá bảng understanding/incidents. Tenant portal chưa đụng.

## Next step chính xác
1. Commit IT-5 (đã sẵn sàng, mọi verify xanh) nếu chưa commit khi đọc handoff này.
2. Mở IT-6: migration PG mới cho command+outcome (`omni_admin`), reconcile Redis↔PG,
   chaos drill kill-agent-giữa-RUNNING + restart gateway.
3. Portal đợt 2 khi user yêu cầu UI tiếp.

## Không được làm lại
IT-1..IT-5 DONE có runtime proof. Portal đợt 1 DONE user-verified. Đừng re-audit portal.
