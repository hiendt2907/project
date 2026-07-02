# Current Priority

Đây là baseline priority — LUÔN đọc `docs/product/PRODUCT_PROOF.md` và runtime thật trước khi chốt
bottleneck cho iteration. Nếu runtime có safety/data-loss/correctness defect, defect đó đứng TRƯỚC
danh sách này.

## Baseline (2026-07-02, theo trạng thái PRODUCT_PROOF.md iteration 5)

1. Repeatable tenant onboarding — Phase 4-7 của slice "Repeatable Tenant Onboarding Baseline" chưa
   chạy (fresh tenant thật, repeatability, operator proof, deploy+observe). Phase 1-3 đã DONE
   (commit `e8a8c96`).
2. ~~Blocker cho #1: `AdminConfigRepo.create_tenant()` không idempotent~~ — **DONE iteration 6**:
   `idempotent=True` opt-in param thêm, test `VERIFIED_TEST`. CHƯA wire vào caller thật cho Phase 4/5.
3. Safe evidence compaction — DONE (commit `e8a8c96`, `src/pkg/reasoning/schema.py`).
4. Canonical Agent provisioning — DONE (commit `e8a8c96`,
   `scripts/lib/remote_agent_provisioning.py`), nhưng CHƯA wire vào `src/remote_agent/agent.py`
   thật (chỉ có sẵn hàm `effective_config_summary()`, chưa log ở agent startup).
5. Fresh tenant replay — chưa làm (phụ thuộc #1/#2).
6. ~~Unknown → Question → Human Claim → Verification — chưa được chứng minh đầy đủ runtime.~~ —
   **VERIFIED_RUNTIME iteration 15**: `POST /onboarding/questions/{id}/answer` chạy thật trên
   `staging-sim` (câu hỏi PENDING thật → ANSWERED → competency facet CLAIMED với evidence_refs
   human:*). Xem PRODUCT_PROOF.md "Iteration 15".
7. UnderstandingComplete — code tồn tại (`compute_readiness()` → `readiness_flag`) nhưng **gap mới
   phát hiện iteration 15**: `business_flow_confirmed_pct` (một trong 3 điều kiện gate) chỉ đọc
   `service_topology.described` do máy set (agent-parsed comment), KHÔNG đọc từ
   `competency_matrix`/Human Claim — nghĩa là trả lời hết Question của tenant không đẩy
   `readiness_flag` tiến gần `true`. Cần quyết định thiết kế trước khi sửa (đọc thêm từ
   competency coverage hay giữ nguyên contract cũ) — chưa làm trong iteration này.
8. ~~Handover — thực ra đã implement (`POST /onboarding/handover-doc`, A8) nhưng CHƯA runtime-verify~~
   — **VERIFIED_RUNTIME iteration 16**: POST thật trên `staging-sim`, diagram version bump
   6747→6752, `GET /onboarding/doc` xác nhận chỉ lưu `content_hash`/`content_length` (không raw
   content) — `INV_DATA_RESIDENCY` giữ nguyên trên pipeline thật. Xem PRODUCT_PROOF.md "Iteration 16".
9. Operator portal — chỉ có API (`GET /onboarding/competency`, `/unknowns`, `/diagram`), chưa có
   UI.
10. Network/dependency topology — Mermaid diagram đã tồn tại và chạy runtime thật
    (`src/pkg/onboarding/discovery_doc.py`, verify version 5605 trên `staging-sim` — xem session
    2026-07-02), nhưng chưa tích hợp UI/portal.
11. M3–M10 curriculum — chưa bắt đầu, ngoài phạm vi golden journey hiện tại.
12. Closed-loop typed operation — code tồn tại (`OMNI_AUTO_EXECUTE_ENABLED=false`, cố ý ngoài
    phạm vi cho tới khi golden journey "sạch").
13. Production hardening — chưa bắt đầu.

## Closed items (không còn là open gap)

- `resolve_scope()` non-admin silent-override — từng ghi là "UX gap chưa fix" (iteration 9). Đã xác
  nhận iteration 13: đây là contract có chủ đích, đã khóa bằng test từ trước
  (`tests/test_tenant_isolation.py::TestResolveScope`, `TestKpiTenantIsolation::
  test_non_admin_cannot_scope_override`). KHÔNG sửa trừ khi có quyết định thiết kế mới rõ ràng.
- "2 agents/2 tenants on 1 VM" isolation — chỉ có live-cluster manual proof (iteration 9). Đã có
  automated regression test iteration 13 (`tests/test_onboarding_pipeline.py::
  TestTwoAgentsTwoTenantsOneVM`).
- Multi-host cho `tenant-replay-01` — từng là leftover cuối của iteration 9. DONE iteration 14: agent
  thứ hai cài trên `cust-app` (bên cạnh agent đã có trên `cust-edge`), Twin gộp fact 2 host thật
  (revision 54→66), isolation với `staging-sim` (cùng share VM `cust-app`) xác nhận không đổi,
  `/onboarding/competency` trả evidence thật cho `host:cust-app`. Test:
  `tests/test_onboarding_pipeline.py::TestOneTenantTwoHosts`. Toàn bộ leftover list iteration 9 nay
  đã đóng.

## Known unrelated risk (P1, không chặn golden journey trực tiếp)

Kafka mọi topic hiện `PartitionCount=1, ReplicationFactor=1` toàn hệ thống — không khớp thiết kế "3
partitions". Xem `docs/post-mortems/drift-correction-2026-07-02.md`. Không tự sửa trừ khi nó chặn
bottleneck đang chọn.

## Cách cập nhật file này

Sau mỗi iteration DONE hoặc PARTIAL, cập nhật danh sách trên để phản ánh đúng trạng thái mới nhất —
đồng bộ với `docs/product/PRODUCT_PROOF.md`. Không để file này lệch khỏi PRODUCT_PROOF quá 1
iteration.
