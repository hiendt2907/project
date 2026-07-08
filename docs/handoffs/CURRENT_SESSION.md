# Current Session Handoff

## Deliverable hiện tại
**Sprint "Nhân viên SRE" (`docs/plans/sprint-agent-sre-employee-production.md`): IT-4 pilot
migration `cust-app` → AOIP employee — CODE + DEPLOY + DRILL DONE, chưa commit.**
- IT-1/IT-2/IT-3 DONE (commit `e64e338`/`cebd84b`/`a364fc2`) — đừng re-verify.
- Branch `main` tại `a364fc2`, working tree DIRTY (toàn bộ IT-4, xem dưới). CHƯA commit/push —
  user chưa yêu cầu.

## Đã hoàn thành trong phiên (IT-4 task 2-4)
1. **`src/aoip/agent/employee.py` (MỚI)** — 1 process 2 vòng: `run_employee()` =
   `asyncio.wait(FIRST_COMPLETED)` trên telemetry (`run_agent(extra_register_fields=
   {"aoip_bundle_sha256": aoip_self_bundle_hash()})`) + daemon (`run_daemon` observe_only).
   Vòng nào xong → cancel vòng kia → propagate result (crash → systemd restart).
2. **`scripts/publish_agent_release.py`**: manifest thêm `aoip_bundle_sha256`.
3. **`scripts/aoip-agent.service` (MỚI)** — theo unit THẬT trên VM (WorkingDirectory=
   /opt/omni-remote-agent, EnvironmentFile=run.env, StateDirectory=aoip, log append).
   **`scripts/omni-agent-bundle.sh`**: rsync thêm `src/aoip` + copy unit mới.
4. **Tests**: `tests/test_aoip_employee_pilot.py` 14/14 GREEN. Full suite **6039 passed,
   2 failed pre-existing/flaky** (routing E2E env-dependent + `test_track2a_k8s_sdk`
   flaky-isolation, pass khi chạy riêng).
5. **Deploy THẬT + verify runtime** (chi tiết PRODUCT_PROOF Iteration 30):
   - Gateway rebuild+rollout, verify code trong pod.
   - cust-app chạy `aoip-agent.service` (employee), 2 vòng poll sống, inbox /var/lib/aoip/inbox.
   - cust-edge/cust-db: ship remote_agent mới (giữ unit legacy) — trước đó drifted THẬT vì
     release 1.2.0 chứa seam IT-4.
   - `make publish-agent-release` → fleet **3/3 current, drifted=0**; cust-app record mang 2 hash.
   - **Rollback drill 2 chiều PASS** (legacy↔employee, record rớt/lấy lại aoip hash đúng).

## Next step chính xác
1. (Nếu user yêu cầu) commit IT-4: toàn bộ file dirty + untracked hiện tại là 1 commit
   `feat(agent): AOIP employee pilot cust-app — 1 process 2 vòng, drift dual-hash (IT-4)`.
2. **So Twin fact cust-app sau ~24h** chạy employee (mốc: migration 2026-07-08 ~14:23 giờ máy):
   `omni:aoip:system_model:staging-sim` — fact cust-app phải tiếp tục tươi (discovery/metrics
   qua employee y như legacy). Nếu tươi → đóng IT-4 hẳn, mở IT-5 (UPDATE_AGENT/updater —
   ACCEPT-GAP duy nhất của parity checklist).

## Gotcha mới phiên này
- **Ship code lên VM PHẢI `COPYFILE_DISABLE=1 tar`** — macOS tar tạo AppleDouble `._*.py`
  trên VM làm lệch bundle hash (đã dính 1 lần, hash lệch hoàn toàn).
- Transfer pattern: tar qua stdin `orb -m <vm> sudo tar -x`, extract vào `remote_agent.new`
  rồi swap (giữ `remote_agent.old` để rollback).
- Endpoint `/webhook/agent/versions` auth bằng Bearer per-agent key (lấy từ run.env cust-app).

## Không được làm lại
- Đừng re-verify IT-1..IT-3, đừng redesign employee/checklist (đã chốt + đã chạy thật).
- Đừng mở rộng `src/remote_agent/` — seam `extra_fields` là ngoại lệ migration duy nhất.
- Kill-switch `OMNI_AUTO_EXECUTE_ENABLED=false` giữ nguyên; employee chạy observe_only.
- Test fail routing E2E (`test_remote_agent_e2e.py::…real_pipeline`) là pre-existing
  env-dependent; `test_track2a_k8s_sdk::…no_snapshot` flaky isolation — đừng "fix".

## Ghi chú vận hành (giữ)
- Sau MỌI lần sửa code agent + deploy VM: `make publish-agent-release`.
- VM access `orb -m <machine>`; unit cust-app nay là `aoip-agent.service`
  (cust-edge/cust-db vẫn `omni-remote-agent.service`).
- Gateway evidence dedup 5-min: re-test phải đổi nội dung envelope.

## Blockers
Không có.

## Tài liệu liên quan
- `docs/product/PRODUCT_PROOF.md` — Iteration 30 (bằng chứng đầy đủ phiên này)
- `docs/plans/it4-collector-parity-checklist.md` — checklist parity đã chốt
- `docs/plans/sprint-agent-sre-employee-production.md` — plan sprint (IT-4 dòng 115-131)
- Memory: `project_sprint_nvsre_it4_employee_pilot.md`
