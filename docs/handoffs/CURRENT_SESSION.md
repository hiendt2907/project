# Current Session Handoff

## Deliverable hiện tại
**Kế hoạch full sprint "Nhân viên SRE" — Remote Agent Production Lifecycle** (phiên planning,
KHÔNG code). File: `docs/plans/sprint-agent-sre-employee-production.md` (mới, chưa commit).
Trạng thái: **PROPOSED — đang chờ user duyệt** trước khi bắt đầu IT-1.
**Đã bổ sung theo yêu cầu user: section "Baseline TRƯỚC sprint" đo thật trên 3 VM + cluster**
(2026-07-07) để so sánh trước/sau — mỗi thước đo sprint có trạng thái ❌/⚠️/❓ kèm lệnh bằng
chứng chạy lại được ở IT-7.

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

## Blockers
Không có blocker kỹ thuật. **Chờ 1 input duy nhất từ user: duyệt sprint plan** (hoặc chỉnh
thứ tự/phạm vi).

## Next step chính xác
1. User duyệt `docs/plans/sprint-agent-sre-employee-production.md`.
2. Commit plan + handoff (khi user cho phép commit).
3. Bắt đầu **IT-1**: sửa `collect_doc_snapshot` hash/sanitize ngay trên VM (payload chỉ còn
   `path/sha256/length/mtime`, bỏ field `content` thô); Omni-side `_sanitize_documents` giữ
   tolerant dual-format; test payload schema; redeploy bundle 3 VM (gotcha: VM bundle từng cũ
   hơn repo); verify bằng Kafka message thật trên topic `omni-knowledge-evidence`.

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
