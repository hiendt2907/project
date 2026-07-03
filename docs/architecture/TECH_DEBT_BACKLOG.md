# Tech Debt Backlog — sweep 2026-07-03

Nguồn: quét toàn repo (3 subagent song song — TODO/FIXME/code-smell trong `src/`, gap trong
`docs/product/PRODUCT_PROOF.md`/ledger/test skip, kiểm invariant CLAUDE.md có bị vi phạm không),
sau khi hoàn tất Phase 0 + quick-win (`ADR-001`, `/readyz`, NetworkPolicy Postgres egress, CI
python-version). Không có file backlog/risk-register tập trung nào tồn tại trước sweep này — đây
là file đầu tiên.

## Đã xử lý trong sweep này

| # | Hạng mục | Xử lý |
|---|---|---|
| 1 | RBAC `omni-fullstack` có quyền `patch`/`update` Secrets, mâu thuẫn comment "write verbs removed" | **Không xoá quyền** — xác nhận đây là tính năng có chủ đích (`k8s_patch_secret` mutate tool, gated bằng `required_evidence`+`MUTATE_TOOL_ALLOWLIST`, dùng cho xoay vòng credential khi remediation SIEM). Sửa comment sai trong `k8s/deployments/omni-fullstack-rbac.yaml` + làm rõ invariant trong `CLAUDE.md`. |
| 2 | Password Postgres hardcode plaintext trong git (`k8s/deployments/omni-postgres.yaml`) | Đã dùng đúng cơ chế K8s Secret (không phải env plaintext trong pod spec), nhưng giá trị bị commit cleartext. Quyết định: **chỉ document** (rotate thật cần restart Postgres + mọi consumer `OMNI_ADMIN_PG_DSN`, rủi ro downtime nếu làm giữa chừng, cần một lượt riêng có thời gian test kỹ). Ghi chú đã thêm trực tiếp vào file manifest. |
| 3 | Debug leftover: `_dbg_log()` ghi log ra path tuyệt đối máy dev cá nhân (`.cursor/debug-*.log`), 2 hàm trùng lặp, ~15 điểm gọi | Xoá hoàn toàn (`src/workers/sdk_service_tools.py`, `src/workers/proactive_observer.py`, `src/workers/proactive_react_runner.py` — bản ở `proactive_react_runner.py` bị bỏ sót lần đầu, gây regression 6 test fail trong `test_cov_proactive_react_runner.py`, đã tìm ra và sửa trong cùng sweep). Xoá test riêng cho `_dbg_log` (`tests/test_cov_proactive_observer_gaps.py`, `tests/test_track2b_diagnostic_proactive.py`). Full suite xanh lại sau fix. |
| 4 | `PRODUCT_PROOF.md`/ledger tưởng như chưa đóng gap readiness-gate iteration 17 | Xác minh lại: `PRODUCT_PROOF.md` và `current-priority.md` **đã cập nhật đầy đủ** từ commit `cf11f1f` — chỉ `AUTONOMOUS_LOOP_LEDGER.md` và `AUTONOMOUS_LOOP_STATE.json` thật sự thiếu checkpoint iteration 17. Đã backfill 2 file này. |
| 5 | CLAUDE.md claim "mọi Kafka topic PartitionCount=1" lỗi thời | Xác minh lại `scripts/kafka_ensure_omni_topics.sh`: `omni-knowledge-evidence`=3, SIEM topic=6, phần còn lại vẫn 1. Cập nhật CLAUDE.md cho khớp thực tế; không còn là drift, chỉ là throughput headroom thấp cho lab (chưa cần sửa gấp). |
| 11 | (phát hiện thêm, không nằm trong 10 mục quét ban đầu) Full test suite ghi đè thật 10 file `docs/post-mortems/*.md` mỗi lần chạy (`workers.archivist.write_incident_postmortem()` mặc định ghi vào `docs/post-mortems/` thật khi `OMNI_POSTMORTEM_DIR` không set) — tái diễn 2 lần trong chính phiên này dù đã "xử lý" bằng cách commit riêng lần đầu | **Sửa gốc**: thêm session-scoped autouse fixture trong `tests/conftest.py` set `OMNI_POSTMORTEM_DIR` sang thư mục scratch cho toàn bộ test session — không còn test nào ghi vào repo docs thật nữa. Xác nhận: chạy lại full suite, `git status docs/post-mortems/` sạch. |
| 12 | (phát hiện phụ) `tests/test_cov_autonomous_feedback.py::TestArchivePostmortem` gọi `_archive_postmortem(...)` (hàm `async def`) mà không `await` — coroutine không bao giờ chạy thật, `RuntimeWarning: coroutine '_archive_postmortem' was never awaited`, test không kiểm tra được gì (false confidence) | Đã sửa: thêm `await` + đổi 3 method sang `async def` (pytest `asyncio_mode=auto` tự nhận, không cần decorator riêng). Xác nhận: không còn RuntimeWarning, 3/3 passed, coroutine chạy thật. |

## Chưa xử lý — cần quyết định/thời gian riêng

| # | Hạng mục | Vì sao chưa làm | Mức ưu tiên |
|---|---|---|---|
| 6 | Phần lớn Kafka topic còn `PartitionCount=1` | Repartition có thể cần recreate topic (mất offset) hoặc `kafka-topics --alter` (chỉ tăng, không giảm) — cần xác nhận tác động tới consumer group đang chạy trước khi đổi trên cluster lab đang sống | Trung bình — throughput/failover risk khi scale, chưa cấp bách ở quy mô lab hiện tại |
| 7 | Không có cơ chế tự động đảm bảo VM mới có đủ discovery flag khi provision (rủi ro tái diễn gap `cust-app` — thiếu `OMNI_REMOTE_DISCOVERY_ENABLED`) | Cần thiết kế provisioning checklist/script mới (`scripts/lib/remote_agent_provisioning.py` đã có nhưng chưa wire kiểm tra flag này), phạm vi lớn hơn một quick-fix | Trung bình — đã xảy ra 1 lần thật, có `effective_config_summary()` sẵn nhưng chưa dùng ở runtime agent (xem `PRODUCT_PROOF.md` dòng ~458) |
| 8 | ~90 chỗ `except Exception: pass` không log, tập trung ở consumer loop chính (`omni_worker.py`), dispatcher (`diagnostic_dispatcher.py`), rollback flow (`rollback_executor.py:135`), mutate-decision logic (`deterministic_mutate_from_evidence.py:336`), health probe (`metrics_exporter.py`) | Khối lượng lớn, sửa mù 90 chỗ rủi ro hơn giá trị — cần review từng nhóm theo mức rủi ro thật (rollback/mutate-decision trước, health-probe sau, phần còn lại thấp ưu tiên) | Cao cho 2-3 chỗ cụ thể (rollback_executor, deterministic_mutate), thấp cho phần còn lại |
| 9 | Duplicate pattern lỗi `except Exception as e: return f"[DATA] error\n[DIAGNOSIS] {e!s}"` lặp 18+ lần trong `k8s_cluster_tools.py`/`k8s_tools.py` | Refactor thành decorator/helper chung — không khẩn, thuần code quality | Thấp |
| 10 | Nghi ngờ dead code: `AutonomyGate` (`src/pkg/autonomy/gate.py:31`) và vài class khác chỉ xuất hiện ở file định nghĩa qua grep tên | Độ tin cậy grep chưa đủ cao (có thể dùng qua alias/Protocol/dynamic import) — xoá mù rủi ro cao hơn lợi ích, cần xác nhận thêm (coverage report hoặc grep sâu hơn theo import graph) trước khi xoá | Thấp — không chặn chức năng, chỉ là dọn dẹp |

## Ghi chú phương pháp

Không sửa mù theo báo cáo subagent — mỗi hạng mục "VIOLATION" hoặc "gap" đều được đọc code thật để
xác nhận trước khi hành động (ví dụ mục 1 tưởng là lỗ hổng nhưng là tính năng thiết kế; mục 4 tưởng
là gap nhưng đã đóng). Runtime/RBAC/Secret đang chạy thật KHÔNG bị động vào mà không hỏi trước
(mục 2).
