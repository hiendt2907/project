# Current Session Handoff

## Deliverable hiện tại
Không có deliverable đang dang dở. Phiên vừa xong gồm 2 việc tuần tự, cả hai đã đóng:
(1) Phase 0 + quick-win từ audit ChatGPT, (2) tech-debt sweep toàn repo.

## Definition of Done
- (1) Quick-win: ADR chốt runtime canonical, `/readyz` runtime-verified trên cluster, NetworkPolicy
  Postgres egress áp thật, CI python-version đồng nhất. **DONE.**
- (2) Tech-debt sweep: quét 3 hướng (TODO/code-smell, doc-gap/test-skip, invariant CLAUDE.md), mỗi
  finding phải đọc code thật trước khi hành động (không sửa mù theo báo cáo subagent), full test
  suite phải xanh sau mọi thay đổi. **DONE.**

## Trạng thái hiện tại
Cả hai việc đã hoàn tất và commit. Không có công việc mở đang chờ tiếp tục.

## Đã hoàn thành
- ADR-001 chốt `aoip.agent.daemon` là target runtime dài hạn; phát hiện phụ: Dockerfile.gateway đã
  COPY `src/aoip/` từ trước, lý do "duplicate" trong `agent_runtime.py` đã lỗi thời.
- `GET /readyz` mới cho gateway (Redis+Postgres check), build+deploy+runtime-verify thật trên
  cluster lab (200 OK qua port-forward).
- NetworkPolicy gateway thêm egress `omni-postgres:5432`, áp thật lên cluster.
- CI `python-version` 3.12→3.13 khớp Dockerfile.
- 10 file `docs/post-mortems/*.md` bị đổi timestamp: xác nhận là hành vi archivist thật (không phải
  bug), commit riêng; sau đó root-cause thật (test không mock `OMNI_POSTMORTEM_DIR`) được tìm và
  sửa bằng session-scoped fixture trong `tests/conftest.py`.
- Xoá debug leftover `_dbg_log()` khỏi 3 file worker (path tuyệt đối máy dev cá nhân) — phát hiện
  và sửa 1 regression thật (6 test fail ở `proactive_react_runner.py`) gây ra bởi việc xoá thiếu sót
  ở lượt đầu.
- Sửa comment RBAC sai lệch (`omni-fullstack-rbac.yaml`) sau khi xác nhận quyền Secrets patch/update
  là tính năng có chủ đích (`k8s_patch_secret` mutate tool), không phải lỗ hổng — KHÔNG thu hồi quyền.
- Làm rõ 2 invariant CLAUDE.md lỗi thời (RBAC Secrets, Kafka partition count).
- Document (không rotate) password Postgres hardcode plaintext trong git — theo lựa chọn người dùng.
- Backfill `AUTONOMOUS_LOOP_LEDGER.md`/`AUTONOMOUS_LOOP_STATE.json` thiếu checkpoint iteration 17.
- Tạo `docs/architecture/TECH_DEBT_BACKLOG.md` — ghi đầy đủ 12 finding, cái nào fixed/documented/deferred.

## Branch và commit
`main`, HEAD `5a546f1` (docs(operations): backfill iteration-17 ledger/state checkpoint, add
tech-debt backlog). 5 commit mới trong phiên: `3d8df9b`, `8194c73`, `d5954cc`, `447e05d`, `5a546f1`.

## Working tree
Sạch, ngoại trừ chính file handoff này đang được cập nhật (`docs/handoffs/CURRENT_SESSION.md`).

## Files chính đã thay đổi
- `docs/architecture/ADR-001-canonical-agent-runtime.md` (new)
- `docs/architecture/TECH_DEBT_BACKLOG.md` (new)
- `src/gateway/api.py` (route `/readyz`)
- `k8s/deployments/omni-gateway.yaml`, `k8s/deployments/omni-fullstack-rbac.yaml`,
  `k8s/deployments/omni-postgres.yaml`
- `src/workers/sdk_service_tools.py`, `src/workers/proactive_observer.py`,
  `src/workers/proactive_react_runner.py`
- `tests/conftest.py` (fixture cô lập postmortem dir)
- `CLAUDE.md`, `docs/vendor/OMNI_PROJECT_CANONICAL.md`
- `docs/operations/AUTONOMOUS_LOOP_LEDGER.md`, `docs/operations/AUTONOMOUS_LOOP_STATE.json`
- `.github/workflows/ci.yml`, `.github/workflows/wiki.yml`

## Quyết định đã chốt
- `aoip.agent.daemon` là canonical agent runtime dài hạn; `remote_agent.agent` giữ nguyên trên VM
  lab, KHÔNG migrate cho tới khi có ADR/kế hoạch riêng (xem ADR-001).
- RBAC `secrets` patch/update trên worker SA là ngoại lệ có chủ đích (backing `k8s_patch_secret`),
  KHÔNG thu hồi trừ khi có quyết định kiến trúc mới.
- Password Postgres KHÔNG rotate trong phiên này — chỉ document, cần một lượt riêng có thời gian
  test kỹ (restart Postgres + mọi consumer `OMNI_ADMIN_PG_DSN`).

## Verification đã chạy
- `pytest tests/ -q --ignore=tests/integration` → 5953 passed, 1 pre-existing flake không liên quan
  (`test_register_then_real_system_metrics_emitted_through_real_pipeline`, phụ thuộc z-score máy
  thật lúc chạy).
- `git status docs/post-mortems/` sạch sau full suite — xác nhận fixture `conftest.py` hoạt động.
- Runtime thật trên cluster: `curl /readyz` → `200 {"redis":"ok","postgres":"ok"}`;
  `kubectl apply --dry-run=server` cho NetworkPolicy → hợp lệ, đã apply thật.

## Deployment hiện tại
`omni-gateway` đã rebuild+redeploy với route `/readyz` mới, đang chạy pod mới trên cluster lab
(namespace `multi-agent`). NetworkPolicy egress Postgres đã áp thật. Không có thay đổi deploy nào
khác treo lại.

## Blockers
None.

## Next step chính xác
Không có việc mở nào cần tiếp tục ngay. Nếu muốn làm tiếp, đọc
`docs/architecture/TECH_DEBT_BACKLOG.md` mục "Chưa xử lý" và chọn 1 trong 4 hạng mục deferred
(Kafka partition, VM provisioning safeguard, except-pass cleanup, dead code) để mở slice mới.

## Lệnh cần chạy lại
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` nếu muốn tái xác nhận trạng thái
xanh trước khi bắt đầu việc mới.

## Không được làm lại
- Không quét lại toàn bộ tech-debt từ đầu — đã có kết quả đầy đủ trong `TECH_DEBT_BACKLOG.md`.
- Không tự ý xoá quyền RBAC Secrets của worker SA — đã xác nhận là tính năng có chủ đích.
- Không rotate password Postgres mà không hỏi trước — quyết định người dùng đã chọn "chỉ document".
- Không suy diễn ADR-001 thành lệnh migrate VM lab ngay — ADR chỉ chốt hướng, không phải thực thi.

## Tài liệu liên quan
- `docs/architecture/ADR-001-canonical-agent-runtime.md`
- `docs/architecture/TECH_DEBT_BACKLOG.md`
- `docs/vendor/OMNI_PROJECT_CANONICAL.md`
- `docs/operations/AUTONOMOUS_LOOP_LEDGER.md`, `docs/operations/AUTONOMOUS_LOOP_STATE.json`
