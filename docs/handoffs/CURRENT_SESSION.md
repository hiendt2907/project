# Current Session Handoff

## Deliverable hiện tại
**Sprint "Nhân viên SRE" (`docs/plans/sprint-agent-sre-employee-production.md`, commit `1e28e85`):
IT-1 + IT-2 DONE (VERIFIED_RUNTIME, đã commit). Kế tiếp: IT-3.**
- IT-1 (data-residency hash tại nguồn) — commit `e64e338`, PRODUCT_PROOF Iteration 27. Metric #1 ✅.
- IT-2 (drift detection bundle-hash) — commit `cebd84b`, PRODUCT_PROOF Iteration 28. Metric #2 ✅.
- Working tree sạch tại `cebd84b` (main). **CHƯA push** — user chưa yêu cầu.

## Bối cảnh quyết định (user đã chốt — KHÔNG re-litigate)
- Ưu tiên: **backend production**, hoàn thiện ý tưởng gốc: "remote agent là nhân viên SRE của
  công ty Omni — tiếp nhận hệ thống khách hàng, tìm hiểu, quan sát, vận hành".
- ADR-001: agent runtime canonical = `aoip.agent.daemon`; `src/remote_agent/` chỉ compat tối
  thiểu, feature mới viết trên `src/aoip/agent/`.
- ADR-002 protocol ĐÃ implement xong (2 phía import `aoip.protocol` + contract test) — đừng re-audit.

## Sprint còn lại (chi tiết trong file plan — nguồn chân lý)
3. **IT-3** Enrollment + identity per-agent trên nền AOIP ← **NEXT**
4. **IT-4** Pilot migration `cust-app` → `aoip.agent.daemon` (parity checklist trước)
5. **IT-5** Update/rollback qua command channel + health-gate + N-1; migrate nốt 2 VM;
   gộp Telegram advisory cho drift (IT-2 mới có API + WARNING log)
6. **IT-6** Command outcome durability — PG source of truth + chaos proof
7. **IT-7** Soak/offline recovery + đóng sprint
8. **IT-8** (stretch) Mission contract skeleton

## Next step chính xác — IT-3
Enroll token 1 lần qua Admin API → credential per-agent (thay `OMNI_AGENT_API_KEY` tĩnh trong
run.env) → tenant binding PG. Gateway endpoint enroll/revoke. Installer dùng
`scripts/lib/remote_agent_provisioning.py`. Nền có sẵn: `src/aoip/agent/identity.py`,
`create_tenant(idempotent=True)`.
**Gotcha FK**: tenant PHẢI provision qua `AdminConfigRepo.create_tenant()` / `POST
/autonomy/tenants` TRƯỚC khi ghi bất kỳ bảng con nào (post-mortem drift-correction-2026-07-02).

## Cơ chế IT-2 phiên mới cần biết (đã VERIFIED_RUNTIME, đừng re-verify)
- `src/remote_agent/bundle_hash.py` — hash canonical (*.py + VERSION, bỏ `__pycache__`, sorted
  posix relpath) chạy CÙNG thuật toán 2 phía: agent self-hash lúc startup ↔
  `scripts/publish_agent_release.py` hash repo.
- Manifest Redis `omni:agent:release_manifest`; publish: `make publish-agent-release`.
- `/webhook/agent/versions` → `drift_status current|drifted|unknown` (`_classify_drift` trong
  `src/gateway/routes/agent_commands.py`) + WARNING `[agent-drift]`. Không report hash = unknown,
  KHÔNG BAO GIỜ current.
- Drill thật cust-db: tamper settings.py → heartbeat ĐẦU TIÊN drifted → restore → current.
- Agent VERSION hiện tại: **1.2.0** (3 VM đã deploy, manifest khớp, cả 3 `current`).

## Ghi chú vận hành (quan trọng)
- **Sau MỌI lần sửa code agent + deploy VM: chạy lại `make publish-agent-release`** — nếu không
  cả 3 agent báo drifted (đúng thiết kế).
- VM access: `orb -m <machine>` (KHÔNG SSH IP). Install dir thật `/opt/omni-remote-agent`,
  unit `omni-remote-agent.service`. Deploy bundle = fresh-copy + dọn `__pycache__` (VM không có
  rsync). Mac FS mount sẵn trong VM tại `/Users/hiendang/...`.
- Gateway evidence dedup 5-min (`omni:evdedup:`): envelope y hệt bị chặn — re-test phải đổi nội dung.
- "test pass + push ≠ deployed": rebuild `make docker-worker` + verify code trong pod trước khi
  coi runtime proof hợp lệ.
- Agent lab khác (loyalty_*, tenant-replay-01_*) chạy 1.1.3 → `unknown` (đúng thiết kế, ngoài scope).
- Kill-switch `OMNI_AUTO_EXECUTE_ENABLED=false` giữ nguyên toàn sprint.

## Verification snapshot
Full suite sau IT-2: **6012 passed, 1 failed** — fail duy nhất
`test_remote_agent_e2e.py::...system_metrics...` (routing knowledge vs diagnostic) là
**pre-existing trên HEAD sạch** (chứng minh bằng `git stash` + rerun). Finding mở, ngoài scope.

## Blockers
Không có.

## Không được làm lại
- Đừng re-audit ADR-002 / re-verify IT-1, IT-2, diagram/CSP/domain cutover phiên trước.
- Đừng mở rộng `src/remote_agent/` cho feature mới (ADR-001).

## Tài liệu liên quan
- `docs/plans/sprint-agent-sre-employee-production.md` — kế hoạch + baseline + 6 metrics
- `docs/product/PRODUCT_PROOF.md` — Iteration 27 (IT-1), 28 (IT-2)
- `docs/product/PRODUCTION_MISSON.md`, `docs/architecture/ADR-001*`, `ADR-002*`
- Memory: `project_autonomous_sre_vision_v2.md`, `project_onboarding_audit_verdict.md`
