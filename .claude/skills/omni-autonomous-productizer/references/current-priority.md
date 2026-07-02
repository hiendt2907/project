# Current Priority

Đây là baseline priority — LUÔN đọc `docs/product/PRODUCT_PROOF.md` và runtime thật trước khi chốt
bottleneck cho iteration. Nếu runtime có safety/data-loss/correctness defect, defect đó đứng TRƯỚC
danh sách này.

## Baseline (2026-07-02, theo trạng thái PRODUCT_PROOF.md iteration 5)

1. Repeatable tenant onboarding — Phase 4-7 của slice "Repeatable Tenant Onboarding Baseline" chưa
   chạy (fresh tenant thật, repeatability, operator proof, deploy+observe). Phase 1-3 đã DONE
   (commit `e8a8c96`).
2. **Blocker cho #1**: `AdminConfigRepo.create_tenant()`
   (`src/services/admin_config/repo.py:574-578`) không idempotent — raise `ValueError` nếu tenant
   tồn tại. Phải sửa trước khi làm Phase 5 (repeat provisioning).
3. Safe evidence compaction — DONE (commit `e8a8c96`, `src/pkg/reasoning/schema.py`).
4. Canonical Agent provisioning — DONE (commit `e8a8c96`,
   `scripts/lib/remote_agent_provisioning.py`), nhưng CHƯA wire vào `src/remote_agent/agent.py`
   thật (chỉ có sẵn hàm `effective_config_summary()`, chưa log ở agent startup).
5. Fresh tenant replay — chưa làm (phụ thuộc #1/#2).
6. Unknown → Question → Human Claim → Verification — chưa được chứng minh đầy đủ runtime.
7. UnderstandingComplete — chưa implement/chưa chứng minh.
8. Handover — chưa implement/chưa chứng minh.
9. Operator portal — chỉ có API (`GET /onboarding/competency`, `/unknowns`, `/diagram`), chưa có
   UI.
10. Network/dependency topology — Mermaid diagram đã tồn tại và chạy runtime thật
    (`src/pkg/onboarding/discovery_doc.py`, verify version 5605 trên `staging-sim` — xem session
    2026-07-02), nhưng chưa tích hợp UI/portal.
11. M3–M10 curriculum — chưa bắt đầu, ngoài phạm vi golden journey hiện tại.
12. Closed-loop typed operation — code tồn tại (`OMNI_AUTO_EXECUTE_ENABLED=false`, cố ý ngoài
    phạm vi cho tới khi golden journey "sạch").
13. Production hardening — chưa bắt đầu.

## Known unrelated risk (P1, không chặn golden journey trực tiếp)

Kafka mọi topic hiện `PartitionCount=1, ReplicationFactor=1` toàn hệ thống — không khớp thiết kế "3
partitions". Xem `docs/post-mortems/drift-correction-2026-07-02.md`. Không tự sửa trừ khi nó chặn
bottleneck đang chọn.

## Cách cập nhật file này

Sau mỗi iteration DONE hoặc PARTIAL, cập nhật danh sách trên để phản ánh đúng trạng thái mới nhất —
đồng bộ với `docs/product/PRODUCT_PROOF.md`. Không để file này lệch khỏi PRODUCT_PROOF quá 1
iteration.
