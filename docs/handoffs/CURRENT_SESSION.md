# Current Session Handoff

## Deliverable hiện tại
**Sprint "Nhân viên SRE" (`docs/plans/sprint-agent-sre-employee-production.md`):
IT-1 + IT-2 + IT-3 DONE (VERIFIED_RUNTIME). Kế tiếp: IT-4 pilot migration `cust-app` → AOIP daemon.**
- IT-1 residency — commit `e64e338`, PRODUCT_PROOF Iteration 27. Metric #1 ✅.
- IT-2 drift detection — commit `cebd84b`, PRODUCT_PROOF Iteration 28. Metric #2 ✅.
- IT-3 enrollment per-agent — PRODUCT_PROOF Iteration 29. Metric #3 ✅. **CHƯA push** (user chưa yêu cầu).

## Bối cảnh quyết định (user đã chốt — KHÔNG re-litigate)
- Ưu tiên: **backend production** — "remote agent là nhân viên SRE của công ty Omni".
- ADR-001: agent runtime canonical = `aoip.agent.daemon`; feature mới trên `src/aoip/agent/`.
- ADR-002 protocol ĐÃ xong — đừng re-audit.

## Cơ chế IT-3 phiên mới cần biết (VERIFIED_RUNTIME 2026-07-08, đừng re-verify)
- PG: migration `0005_agent_enrollment.sql` — `omni_admin.agent_enroll_token` (one-time, sha256)
  + `omni_admin.agent_credential` (per-agent, unique-active per (tenant,agent)).
- Repo: `AdminConfigRepo.consume_enroll_token_and_issue_credential` — 1 TX atomic, UPDATE
  điều kiện `status='issued'` = single-use (chống race); `revoke_agent_credentials` trả
  key_hash để DEL cache `omni:agentcred:cache:{hash}` → 401 tức thì.
- Gateway: `POST /webhook/agent/enroll` (router riêng, KHÔNG bearer guard, rate-limit 10/min/IP);
  Admin API `/autonomy/tenants/{tid}/enroll-tokens` + `agent-credentials` — **admin-only**
  (`_require_admin_ctx` trong autonomy.py; per-agent key → 403).
- `_require_api_key` fallback: sha256(key) → Redis cache 60s → PG → TenantContext(is_admin=False).
- Client: `src/aoip/agent/enrollment.py` (ADR-001). Installer: `scripts/enroll_remote_agent.py`
  (canonical `remote_agent_provisioning`, orb push chmod 600, idempotent rewrite skip).
- **Trạng thái VM**: cust-app chạy per-agent credential (`J-LIR3jl…`, PG id=1, agent_id
  `staging-sim_cust-app`); cust-edge/cust-db VẪN key tenant-shared cũ qua env
  `OMNI_TENANT_APIKEYS` (transition có chủ đích — chuyển khi IT-4/IT-5). Credential rotation
  = non-goal (risk register).
- run.env cust-app nay có `OMNI_AGENT_TENANT_ID=staging-sim` tường minh (trước đây thiếu).

## Sprint còn lại (plan file = nguồn chân lý)
4. **IT-4** Pilot migration `cust-app` → `aoip.agent.daemon` ← **NEXT** (parity checklist
   collectors TRƯỚC khi chạm VM; unit song song, rollback = switch unit cũ; so Twin fact 24h)
5. **IT-5** Update/rollback qua command channel + health-gate + N-1; migrate nốt 2 VM;
   Telegram advisory cho drift
6. **IT-6** Command outcome durability PG + chaos proof
7. **IT-7** Soak/offline recovery + đóng sprint · 8. IT-8 (stretch) Mission contract

## Ghi chú vận hành
- Sau MỌI lần sửa code agent + deploy VM: `make publish-agent-release` (không thì cả 3 báo drifted).
- VM access `orb -m <machine>`; install dir `/opt/omni-remote-agent`; unit `omni-remote-agent.service`.
- Gateway image: `make docker-gateway && make deploy-gateway`; verify code TRONG pod trước khi
  coi runtime proof hợp lệ. Admin key: secret `omni-gateway-secret` field `OMNI_ADMIN_API_KEYS`.
- Kill-switch `OMNI_AUTO_EXECUTE_ENABLED=false` giữ nguyên toàn sprint.
- Gateway evidence dedup 5-min: re-test phải đổi nội dung envelope.

## Verification snapshot
Full suite sau IT-3: **6026 passed, 0 failed** (fail routing E2E pre-existing của phiên trước
không tái hiện lần chạy này). `tests/test_agent_enrollment.py` 14/14.

## Blockers
Không có.

## Không được làm lại
- Đừng re-audit ADR-002 / re-verify IT-1..IT-3.
- Đừng mở rộng `src/remote_agent/` cho feature mới (ADR-001).

## Tài liệu liên quan
- `docs/plans/sprint-agent-sre-employee-production.md` — plan + baseline 6 metrics (3/6 ✅)
- `docs/product/PRODUCT_PROOF.md` — Iteration 27/28/29
- Memory: `project_autonomous_sre_vision_v2.md`, `project_onboarding_audit_verdict.md`
