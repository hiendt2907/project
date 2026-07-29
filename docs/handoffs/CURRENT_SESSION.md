# Current Session Handoff

## Deliverable hiện tại
Tiến hoá Omni thành Autonomous SRE đa tenant: 4 gap G1–G4 (RAG vector, learning loop, KPI
key, capacity/report) + audit API/UI/portal + dọn dẹp repo. **Toàn bộ code đã xong, CHƯA COMMIT.**

## Definition of Done
- G1–G4 có module + loop + endpoint, test xanh.
- Lỗ hổng phân quyền tìm được phải vá và verify trên cluster thật (403/200).
- Repo sạch dead code/dead docs, test suite không giảm.
- → **Đạt hết**, chỉ còn bước commit.

## Trạng thái hiện tại
Working tree đầy đủ thay đổi, test 6736 passed / 0 fail. Gateway đã rebuild+redeploy và verify
bằng curl. Chờ chỉ thị commit.

## Đã hoàn thành

### G1–G4 (module mới, đều untracked)
| Gap | Root cause thật (không phải suy đoán) | Fix |
|---|---|---|
| G1 RAG | `advisory_ingest.py` docstring nói dối — chưa từng ghi vector store. HLEN 1019 là **tín hiệu giả** | `src/training/advisory_sop_vector.py` backfill hash → HNSW, payload ép `auto_execute=False` |
| G2 Learning | `omni:learn:promo:*` rỗng vì kill-switch false → 0 mutation → 0 VERIFIED_SUCCESS → promoter không bao giờ chạy. **"Omni không học được vì nó không hành động."** | `advisory_promoter.py` học từ operator ack (tín hiệu duy nhất có trong shadow) + `bump_playbook_graduation()` cho bảng vốn mồ côi |
| G3 KPI | reader/writer lệch key → mọi lần đọc FP-rate thấy 0 mẫu → luôn đi nhánh fail-open | `kpi_outcome_key()` một nguồn sự thật; `gate.py` no-data → trả **1.0** (fail-closed) |
| G4 Capacity/Report | không có mặt tiền | `pkg/reasoning/{capacity_advisor,sre_report}.py` (pure, no I/O) + `workers/capacity_loops.py` hourly + `gateway/routes/reports.py` |

`tier_readiness.py` thêm gate `graduated_playbooks` (default 0 = fail-closed).

### 3 lỗ hổng bảo mật — đã vá + verify trên cluster
1. **`/autonomy` không phân quyền** — 21/27 endpoint thiếu. Đã **khai thác thật**: key
   `staging-sim` (non-admin) tạo được API key cho tenant `default`. Trung thực về mức độ:
   key sinh ra KHÔNG auth được (`_require_api_key` chỉ tra env keys + `agent_credential`),
   nên chưa phải auth-bypass toàn phần — nhưng vẫn ghi bậy registry + đọc chéo control-plane.
   Artifact pentest đã xoá khỏi PG. Vá: `_require_admin_ctx` cho 10 endpoint → 403/200 verify OK.
2. **`POST /trace/purge`** xoá global, không đòi admin → gate `is_admin_ctx`, verify 403.
3. **CRAT export/stats đọc chéo tenant** (dữ liệu SOX/PCI) → `_effective_tenant()`.
   **Gotcha**: `resolve_scope` trả None cho CẢ `ctx is None` (lab) lẫn admin-không-override;
   gộp 2 nghĩa làm hỏng 2 test lab. Phải tách: lab → tôn trọng tenant hỏi; admin → `"default"`.

### Dọn dẹp repo — 20 file tracked + ~73MB rác
`src/workers/adapters/` (4), `src/knowledge/enrich.py`, `src/prober/clinical.py`,
`src/sre/watchdog.py`, `deployments/` root (6), 7 stub provider-portal, `SectionStub`;
`htmlcov-gate/` 15M + `wiki/site/` 58M + `dist/` + `evidence/` (đã bổ sung `.gitignore`).
Sửa thay vì xoá: `docs/CODEBASE.md` (overview còn mô tả split-role + brain-go như đang chạy),
runbook `omni-analyst` → `omni-fullstack`, 5 comment `schema.sql` (pgvector đã gỡ).

## Branch và commit
`main` @ `da83c81` — **chưa có commit mới nào của phiên này**.

## Working tree
30 modified · 20 deleted · 22 untracked. Không clean, cố ý.

## Files chính đã thay đổi
Mới: `src/training/advisory_sop_vector.py`, `src/services/learning_promoter/advisory_promoter.py`,
`src/pkg/reasoning/{capacity_advisor,sre_report}.py`, `src/workers/capacity_loops.py`,
`src/gateway/routes/reports.py`, `ui/apps/provider-portal/EXCLUDED_ROUTES.md`, 10 file test.
Sửa: `gateway/routes/{autonomy,trace,compliance}.py`, `pkg/autonomy/gate.py`,
`workers/{kpi_metrics,tier_readiness,tier_loops,omni_worker,settings}.py`,
`services/{admin_config/repo,learning_promoter/promoter}.py`.

## Quyết định đã chốt (KHÔNG thiết kế lại)
- `advisory_sop_payload()` **luôn** `auto_execute=False` — vì `resolve_remediation_from_memory()`
  (`handlers.py:663`) THỰC THI tool khi cờ này true.
- Đã xoá `src/pkg/autonomy/tier_readiness.py` do tôi tạo — trùng và kém hơn
  `src/workers/tier_readiness.py` có sẵn. Đừng tạo lại.
- **KHÔNG xoá** (subagent đề xuất nhưng xác minh ngược lại):
  `docs/handoffs/PHASE_0_6_PROGRESS.md` + `docs/reports/frontend-backend-logic-verification-2026-07-14.md`
  (9 tham chiếu, gồm `src/pkg/contracts/identity.py:13` và ADR-006);
  `src/aoip/capabilities/systemd_*.py` (feature `63b20c5` chưa đấu dây, không phải rác);
  `smart-siem/` (git submodule); 18 file ontology `docs/architecture/`.
- 7 stub provider bị xoá route nhưng **lý do loại trừ giữ nguyên văn** ở
  `ui/apps/provider-portal/EXCLUDED_ROUTES.md` — đặc biệt `/policies` là rào AN TOÀN thật.

## Verification đã chạy
- `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` → **6736 passed, 0 fail**
  (chạy lại lúc chốt handoff, 161s).
- AST parse toàn `src/` sạch; UI typecheck sạch.
- curl trên cluster: 4 đường tấn công `/autonomy` → 403, admin → 200; `/trace/purge` → 403;
  CRAT `?tenant_id=default` từ staging-sim → body trả `tenant_id: staging-sim`.

## Deployment hiện tại
`omni-gateway` đã rebuild+redeploy (`sha256:ad768b96e57c...`) kèm 3 bản vá bảo mật.
`omni-fullstack` **chưa** redeploy với `capacity_loops` — loop mới chưa chạy trên cluster.

## Blockers
None.

## Next step chính xác
Commit theo 3 nhóm (chờ user cho phép — chưa được chỉ thị):
1. `fix(gateway): vá 3 lỗ hổng phân quyền — /autonomy, /trace/purge, CRAT cross-tenant`
2. `feat(sre): G1-G4 — RAG backfill, advisory promoter, KPI key contract, capacity/report`
3. `chore: dọn dead code/dead docs/route rỗng (20 file)`

Sau commit: `make deploy-worker` để `capacity_report_loop` thật sự chạy.

## Lệnh cần chạy lại
```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
make deploy-worker deploy-gateway
```

## Không được làm lại
Audit portal/gateway (3 subagent đã chạy), khảo sát dead code (2 subagent), pentest `/autonomy`
(đã khai thác + vá + dọn artifact), backfill RAG vector (đã chạy thật).

## Việc còn mở (không thuộc deliverable này)
- `/autonomy/hitl/pending` + `/decide` **chưa scope tenant** — phải vá TRƯỚC khi nối tenant
  portal, nếu không tạo leak cross-tenant mới.
- Tenant portal đi qua `src/aoip/console/app.py`, KHÔNG qua gateway → endpoint reports/HITL
  phải thêm ở đó, chưa làm.
- `/approvals` khai báo `string[]` nhưng backend trả `list[dict]` → React crash runtime.

## Tài liệu liên quan
`plans/omni-evolve-to-senior-sre-2026-07-29.md` · `ui/apps/provider-portal/EXCLUDED_ROUTES.md` ·
`docs/CODEBASE.md` · `docs/architecture/ASSESSMENT_autonomous_sre_v2.md`
