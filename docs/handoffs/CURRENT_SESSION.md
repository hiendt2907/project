# Current Session Handoff

## Deliverable hiện tại
**Sprint "Nhân viên SRE" ĐÃ DUYỆT + IT-1 ĐÃ HOÀN THÀNH (VERIFIED_RUNTIME).**
- Plan + baseline: `docs/plans/sprint-agent-sre-employee-production.md` (commit `1e28e85`).
- IT-1 (data-residency tại nguồn) DONE: agent 1.2.0 hash sha256+length+mtime ngay trên VM,
  field `content` không còn rời VM khách. Chi tiết + evidence đầy đủ: PRODUCT_PROOF
  **Iteration 27**. Sprint metric #1: ❌ → ✅.

## Bối cảnh quyết định (user chốt trong phiên này)
- User chuyển hướng ưu tiên: **backend production**, tập trung hoàn thiện backend + ý tưởng gốc.
- Ý tưởng gốc user nhắc lại nguyên văn: "remote agent là nhân viên SRE của công ty Omni, nó sẽ
  tiếp nhận hệ thống của khách hàng, tìm hiểu, quan sát, vận hành" — khớp Domain Model 2026-06-29
  ("Agent = identity SRE, collector chỉ là tool").
- Deliverable phiên trước (topology diagram + domain cutover) đã DONE, commit + push xong
  (HEAD `359d7c1`) — KHÔNG re-verify lại.

## Nội dung sprint (tóm tắt — chi tiết đọc file plan)
7 iteration + 1 stretch, mỗi iteration là vertical slice có runtime proof:
1. **IT-1** Data-residency fix tại nguồn (`collect_doc_snapshot` hash trên VM, bỏ raw `content`)
2. **IT-2** Drift detection (version + bundle hash vs release manifest)
3. **IT-3** Enrollment + identity per-agent trên nền AOIP
4. **IT-4** Pilot migration `cust-app` → `aoip.agent.daemon` (parity checklist bắt buộc trước)
5. **IT-5** Update/rollback qua command channel, health-gate, N-1 bundle → migrate nốt 2 VM
6. **IT-6** Command outcome durability — PG source of truth + chaos proof
7. **IT-7** Soak/offline recovery + đóng sprint (cập nhật PRODUCT_PROOF)
8. **IT-8** (stretch) Mission contract skeleton

6 thước đo cuối sprint ghi trong file plan — đều yêu cầu runtime proof.

## Phát hiện khảo sát trong phiên (tiết kiệm công phiên sau — ĐÃ verify trên repo thật)
- **ADR-002 đã implement xong**: `src/gateway/routes/agent_runtime.py:69` và
  `src/aoip/agent/delivery.py:31` đều import `aoip.protocol`; đã có
  `tests/test_aoip_protocol_contract.py`. KHÔNG lặp lại việc này trong sprint.
- Nền có sẵn cho IT-3/IT-5: `src/aoip/agent/identity.py`, `src/remote_agent/updater.py`
  (download+sha256+extract+restart, CHƯA có health-gate/rollback),
  `scripts/lib/remote_agent_provisioning.py`, `create_tenant(idempotent=True)`.
- Gap residency xác nhận còn nguyên: `src/remote_agent/collectors/discovery_evidence.py:209-210`
  gửi raw `content` (≤8000 byte × ≤20 file).
- Agent VERSION hiện tại: `1.1.3` (`src/remote_agent/VERSION`); endpoint
  `/webhook/agent/versions` (iteration 25) mới chỉ list, chưa so sánh expected.

## Working tree
```
 M docs/handoffs/CURRENT_SESSION.md          (file này — rewrite cho phiên planning)
?? docs/plans/sprint-agent-sre-employee-production.md   (kế hoạch sprint, MỚI)
```
Chưa commit gì trong phiên này. HEAD vẫn `359d7c1` (main, đã push từ phiên trước).

## Files changed phiên này
- `docs/plans/sprint-agent-sre-employee-production.md` — MỚI, toàn bộ kế hoạch sprint
- `docs/handoffs/CURRENT_SESSION.md` — rewrite

## Verification đã chạy (baseline measurement — read-only, không mutate gì)
- `orb -m cust-edge/app/db`: unit `omni-remote-agent.service` active cả 3; `aoip-agent` chưa
  từng chạy; VERSION `1.1.3` khớp repo cả 3; dòng raw-content `discovery_evidence.py:209-210`
  sống trên cả 3 VM (install dir thật = `/opt/omni-remote-agent`, KHÔNG phải `/opt/omni-agent`
  như `omni-agent-install.sh` khai — hai installer khác nhau, unit lấy từ `systemctl cat`).
- `kubectl exec redis-0`: `omni:cmd:*` = 0 key (command state Redis-only); Twin 2 tenant
  (`staging-sim`, `tenant-replay-01`, HLEN=3 mỗi cái); snapshot at-rest sạch (không field
  `content`); `tenant-replay-01` đã có agent profile cust-edge + cust-app.
- `migrations/omni_admin/0001-0004`: không có bảng command/outcome nào trong PG.
Kết quả đầy đủ ghi trong section "Baseline TRƯỚC sprint" của file plan.

## IT-1 — files changed (code)
- `src/remote_agent/collectors/discovery_evidence.py` — hash tại nguồn, bỏ raw content
- `src/remote_agent/VERSION` — 1.1.3 → 1.2.0
- `src/pkg/onboarding/discovery_doc.py::_sanitize_documents` — dual-format + WARNING legacy
- `src/aoip/onboarding_projection.py` — doc node id từ `content_hash[:16]` (id ổn định, chống
  collapse về hash-rỗng)
- Tests: `test_remote_agent.py`, `test_onboarding_pipeline.py`, `test_aoip_onboarding_projection.py`

## IT-1 — verification đã chạy
- Full suite: 5999 passed, 1 failed — fail `test_remote_agent_e2e.py::...system_metrics...`
  (topic routing knowledge vs diagnostic) **pre-existing trên HEAD sạch, chứng minh bằng
  `git stash` + rerun**. KHÔNG do IT-1. Cần điều tra riêng (finding mở).
- Runtime: bundle deploy 3 VM (VERSION 1.2.0, service active); rebuild `make docker-worker` +
  rollout omni-fullstack/omni-onboarding (bắt được drift image cũ giữa chừng — bản ghi đầu
  hash-of-empty trước rollout, đúng bài học iteration 1); canary `/srv/README.md` (marker
  XYZZY) trên cust-edge → `HGET omni:onboarding:doc:staging-sim doc_snapshot` = hash khớp
  `sha256sum` trên VM + mtime + không `content`; sweep toàn Redis: 0 leak.
- Gotcha tái xác nhận: gateway `dedup_skip` fingerprint 5-min window — envelope doc không đổi
  sẽ bị chặn; muốn re-test phải đổi nội dung file canary.

## Blockers
Không có. Finding mở (ngoài scope IT-1): E2E test routing fail pre-existing (xem trên).

## Next step chính xác
1. Commit IT-1 (code + tests + PRODUCT_PROOF + plan + handoff) — đang làm cuối phiên này.
2. Bắt đầu **IT-2 (drift detection)**: release manifest (version + bundle sha256 expected),
   agent gửi bundle hash trong heartbeat, gateway so sánh → `current|drifted|unknown` trên
   `/webhook/agent/versions` + Telegram advisory. Chi tiết trong sprint plan.
3. (Tuỳ chọn, nếu rảnh) Điều tra fail pre-existing `test_remote_agent_e2e.py` routing.

## Không được làm lại
- Đừng re-audit ADR-002/protocol vocabulary — đã xong, có contract test.
- Đừng re-verify diagram/CSP/domain cutover của phiên trước.
- Đừng mở rộng `src/remote_agent/` cho feature mới — chỉ compat tối thiểu (ADR-001);
  feature agent mới viết trên `src/aoip/agent/`.

## Tài liệu liên quan
- `docs/plans/sprint-agent-sre-employee-production.md` (kế hoạch sprint — nguồn chân lý phiên tới)
- `docs/product/PRODUCTION_MISSON.md` (mission gốc, ưu tiên 1-8)
- `docs/architecture/ADR-001-canonical-agent-runtime.md`, `ADR-002-command-protocol.md`
- Memory: `project_autonomous_sre_vision_v2.md`, `project_onboarding_audit_verdict.md`,
  `project_data_residency_onboarding_agent.md` (một phần outdated — phía Omni đã fix, chỉ còn
  phía agent)
