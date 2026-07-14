# Current Session Handoff

## Deliverable hiện tại (2026-07-14)
**Customer System Understanding + portal topology — DONE, đang commit/push theo chỉ thị user.**

### Canonical decisions
- System Twin primary view is a customer-only graph canvas, not a card list. Omni and
  Remote Agent are operators and never appear as customer topology nodes.
- Primary graph uses observed host/service/port/connection facts and filters Linux
  platform noise from the visual layer; raw evidence remains auditable.
- API sequence is contract-first. Remote Agent discovers bounded OpenAPI/Swagger files
  or the user uploads one. Parsing emits metadata only. Access-log metadata verifies
  runtime routes; TCP-only evidence is `network_only`, never HTTP.
- Read-model statuses: `runtime_verified`, `contract_observed`, `missing_contract`,
  `network_only`.

### Relevant implementation
- `src/remote_agent/collectors/api_contract.py`: local OpenAPI v2/v3 + Swagger JSON/YAML metadata parser.
- `src/remote_agent/collectors/logs.py`: redacted access-log method/route/status aggregation.
- `src/gateway/routes/onboarding.py`: contract/runtime correlation in `/onboarding/system-twin`.
- `src/workers/onboarding_pipeline.py`: asks for OpenAPI/Swagger when access routes lack a contract.
- `ui/apps/provider-portal/app/understanding/SystemTwinPanel.tsx`: SVG customer topology graph.
- `docs/architecture/customer-system-understanding.md`: canonical evidence and UI contract.

### Verification/deployment
- Contract/read-model/onboarding tests: 51 passed.
- Remote Agent regression: 117 passed.
- Provider UI ESLint/build passed; gateway, worker and provider UI rolled out.
- Live provider HTTP 200; live staging remains correctly `network_only` until real
  customer OpenAPI/Swagger or access-log evidence is present.

## Historical IT-7 checkpoint (2026-07-13)
**IT-7 (soak + offline recovery) — DONE, SPRINT NV-SRE ĐÓNG 6/6 metric.**
PRODUCT_PROOF Iteration 33 đã ghi đầy đủ — đừng re-verify. Tóm tắt:
- **Evidence outbox** `src/remote_agent/outbox.py` (mới): spool disk `/var/lib/aoip/outbox`
  khi emit fail toàn phần; flush cũ→mới, stop-on-failure, no-duplicate by design.
  `emitter.emit()` contract mới: `None` = transport fail (spool), `0` = gateway enqueue 0.
  Wire trong `agent.py` (flush trước emit mới). Env `OMNI_AGENT_OUTBOX_DIR`.
- **Multi-host knowledge fix** `src/pkg/onboarding/discovery_doc.py`: accumulate per-host
  slot `{probe}@{hostname}` + merge khi đọc (union list, dedupe JSON canonical, field
  `hosts`) — fix TC-OB12 thật (3 host ghi đè fact nhau); caller `onboarding_pipeline.py`
  truyền hostname. Legacy slot backward-compatible.
- 2 test-bug e2e sửa: TC-OB07 (thiết kế Iteration 22 là `graph LR`, không phải
  sequenceDiagram), TC-OB10 (lấy câu hỏi từ open zset thay vì hash).
- Agent release **1.3.2** publish + update fleet 3/3 bằng cơ chế IT-5
  (`upd-1-3-2-*` COMPLETED/updated, `/versions` current drifted=0).
- Drill: reboot cả 3 VM → auto-active ≤30s; cắt mạng 10' từng VM (iptables DROP) →
  mỗi VM spool 3 batch → flush đủ, pending=0. e2e 10/10 TC PASS.
- Test: `tests/test_remote_agent_outbox.py` (8 mới), +2 merge test
  `test_onboarding_pipeline.py`; full suite **6097 passed** (2 test cũ cập nhật theo
  contract emit None: `test_cov_remote_agent_collectors.py`, `test_remote_agent_e2e.py`).
- Deploy: worker + gateway rebuild/rollout (verify signature trong pod); VM verify
  outbox.py có thật trên cust-app.
- Docs: sprint plan bảng cột SAU 6/6 ✅; ADR-001 migration DONE; PRODUCT_PROOF Iter 33.

## Changed files (chưa commit)
`src/remote_agent/{outbox.py(new),emitter.py,agent.py,VERSION}` ·
`src/pkg/onboarding/discovery_doc.py` · `src/workers/onboarding_pipeline.py` ·
`scripts/e2e_onboarding_full_flow.py` · `tests/test_remote_agent_outbox.py(new)` ·
`tests/{test_onboarding_pipeline,test_cov_remote_agent_collectors,test_remote_agent_e2e}.py` ·
`docs/plans/sprint-agent-sre-employee-production.md` ·
`docs/architecture/ADR-001-canonical-agent-runtime.md` · `docs/product/PRODUCT_PROOF.md`

## Known behavior / quan sát treo
- Open onboarding questions ~2974 (flood tích lũy từ gap detection) — đáng dọn iteration sau.
- Registry `tenant-replay-01` (2 record 1.1.3 unknown) vẫn "online" — truy nguồn heartbeat sau.
- Spool cadence khi offline ~4 phút/batch (retry timeout tuần tự) — chấp nhận được.

## Trạng thái sprint NV-SRE
**IT-1..IT-7 DONE — SPRINT ĐÓNG 6/6 metric Passed.** IT-8 (stretch, mission skeleton) là
optional — chỉ làm nếu user yêu cầu.

## Trạng thái Git tại checkpoint
Branch `main`, HEAD `47baf01`. Working tree có toàn bộ thay đổi IT-7 CHƯA COMMIT
(+ untracked `.claude/launch.json`). Verification cuối: full suite 6097 passed,
e2e 10/10, drill runtime proof trong PRODUCT_PROOF Iteration 33.

## Next step chính xác
1. Commit + push IT-7 khi user chỉ thị (theo quy ước 1 commit/iteration).
2. IT-8 stretch (mission skeleton) hoặc portal đợt 2 — chờ user chọn.

## Không được làm lại
IT-1..IT-7 DONE có runtime proof (Iteration 27-33). Đừng re-run drill cắt mạng/reboot.
Portal đợt 1 DONE user-verified.
