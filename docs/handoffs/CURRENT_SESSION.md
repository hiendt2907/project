# Current Session Handoff

## Deliverable hiện tại (2026-07-13 tối)
**IT-6 (command outcome durability PG + chaos proof) — DONE, ĐÃ COMMIT `8e7d737` + PUSH.**
PRODUCT_PROOF Iteration 32 đã ghi đầy đủ — đừng re-verify. Tóm tắt:
- Migration `migrations/omni_admin/0006_agent_command_ledger.sql` — bảng
  `omni_admin.agent_command_outcome`, PK (tenant_id, command_id), KHÔNG FK tenant (chủ ý,
  ledger sự-kiện-đã-xảy-ra; lý do trong comment SQL).
- `src/services/agent_command_ledger/ledger.py`: pg_record_enqueue / pg_record_terminal
  (UPSERT first-writer-wins `WHERE terminal_at IS NULL`) / reconcile_commands_from_redis
  (SCAN `omni:cmd:rec:*`, chạy mỗi gateway startup — `src/gateway/api.py` lifespan).
- Wire hot-path best-effort trong `src/gateway/routes/agent_runtime.py` (enqueue, terminal,
  heartbeat-expire, claim-expire mirror). PG lỗi → log + vẫn ACK agent, reconciler backfill.
- Gotcha lặp lại Iteration 3: `Dockerfile.gateway` phải COPY module services mới — đã thêm
  dòng COPY agent_command_ledger.
- Chaos drill THẬT: enqueue `upd-it6-chaos` (UPDATE_AGENT re-install 1.3.1 cust-app) →
  RUNNING (agent chết by design) → rollout restart gateway đúng lúc đó → PG
  `count=1 COMPLETED/updated source=gateway`; reconcile boot đầu backfill 7 record IT-5
  (`recorded: 7`), boot hai `already_terminal: 7, inserted_open: 1` — không ghi đè.
- Test: `tests/test_agent_command_pg_ledger.py` (9 mới); nhóm
  gateway/runtime/ledger/enroll/updater 316 passed.

## Deliverable cùng ngày (chiều)
**Anti-hallucination fix sau false-incident /mnt/mac — DONE, ĐÃ COMMIT `7f28d5d` + PUSH.**
Post-mortem: card REMOTE DIAG cust-edge báo "disk 95% + inode exhaustion confirmed" +
remediation `truncate /mnt/mac/vmware/hostd.log` (file không tồn tại). Root cause KÉP:
(1) tín hiệu gốc = unit cũ `omni-remote-agent` disabled-nhưng-failed (residue migration IT-5);
(2) LLM 7B **parrot nguyên văn ví dụ few-shot trong system prompt** của
`src/services/analyst/diagnosis_loop.py` (ví dụ cũ chứa đúng "inode exhaustion confirmed" +
"/var/log/vmware/hostd.log"). 3 fix, đều đã deploy + verify runtime:
1. `diagnosis_loop.py`: gỡ ví dụ parrotable khỏi prompt, thêm rule GROUNDING + host-mount
   out-of-scope; thêm **grounding gate code** `_apply_grounding_gate()` (INV_DIAG_GROUNDED):
   path/percentage trong root_cause/remediation phải có verbatim trong evidence corpus của
   session, vi phạm → drop step + prefix [UNVERIFIED:] + cap confidence 0.3. Worker image
   rebuild + rollout, đã exec-check trong pod (gate OK, prompt clean).
2. `remote_agent/collectors/storage.py`: skip host-share mount (virtiofs/9p/... +
   prefix /mnt/mac,/mnt/machines) khỏi disk+inode alert, ghi `host_share_excluded`.
3. `remote_agent/collectors/services.py`: unit disabled/masked + failed →
   `ignored_disabled_units`, không alert. 3 VM đã `sudo systemctl reset-failed`.
Agent release **1.3.1** publish + update fleet BẰNG cơ chế IT-5: 3/3 COMPLETED/updated,
`/versions` 3/3 current drifted=0; code mới verify trực tiếp trên cust-edge
(`/opt/omni-remote-agent`). Test: `tests/test_diag_grounding_and_scope.py` (9 test mới),
972 pass nhóm diag/remote/storage/collector; 1 fail `test_remote_agent_e2e.py::...real_pipeline`
là PRE-EXISTING (fail cả trên HEAD sạch — routing phụ thuộc metrics host thật).

## Deliverable trước đó
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
IT-1..IT-6 DONE. Còn: **IT-7** (soak + offline recovery:
reboot/cắt mạng 10p từng VM, chạy lại e2e_onboarding_full_flow 10 TC, điền cột SAU 6 metric
sprint, cập nhật ADR-001) → IT-8 stretch (mission skeleton).

## Portal (đợt 1 DONE — c27c86a + dd9f9c9, 18/18 E2E)
Gap đợt 2 (user muốn "mọi backend hiển thị, non-tech hiểu"): incident drill-down (console
`/incident/{tenant}/{cid}` có sẵn), advisory/brain card VI, support-access, workers health,
KB stats, Việt hoá bảng understanding/incidents. Tenant portal chưa đụng.

## Trạng thái Git tại checkpoint (2026-07-13 tối)
Branch `main`, HEAD `8e7d737` (IT-6) trên `7f28d5d` (anti-hallucination fix) — CẢ HAI ĐÃ PUSH.
Working tree sạch, chỉ còn untracked `.claude/launch.json` (dev tooling, không thuộc sản phẩm).
Verification cuối: nhóm gateway/runtime/ledger/enroll/updater 316 passed + chaos drill PG count=1.

## Next step chính xác
1. Mở IT-7 (soak + offline recovery): reboot/cắt mạng 10p từng VM, chạy lại
   e2e_onboarding_full_flow 10 TC, điền cột SAU 6 metric sprint, cập nhật ADR-001.
2. Portal đợt 2 khi user yêu cầu UI tiếp (bảng gap ở mục Portal trên).

## Không được làm lại
IT-1..IT-6 DONE có runtime proof (IT-6: PRODUCT_PROOF Iteration 32). Portal đợt 1 DONE
user-verified. Đừng re-audit portal.
