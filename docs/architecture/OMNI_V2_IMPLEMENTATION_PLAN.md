# Omni v2 — Implementation Plan

**Ngày:** 2026-08-03 · **Trạng thái:** Kế hoạch triển khai, chờ duyệt từng Workstream (WS) trước khi code
**Input:** `OMNI_V2_ARCHITECTURE_REDESIGN.md` (Phase 1-10) + `OMNI_AUTONOMOUS_AGENT_PLATFORM_REVIEW.md` (5 trục autonomy). Đây là bản gộp 2 tài liệu đó thành 1 roadmap thực thi được, có thứ tự phụ thuộc rõ ràng, mỗi Workstream deploy độc lập.

**Nguyên tắc triển khai:** mỗi WS phải (1) giữ nguyên 7348 test xanh, (2) không phá bất kỳ capability đã verify runtime, (3) có thể rollback bằng revert commit hoặc scale-về-0 (không có "point of no return" giữa các WS), (4) đi qua đúng quy trình dự án: EXPLORE (đã xong ở Đ13/Đ14) → PLAN (tài liệu này) → TDD → code-review → **GIT chỉ khi được chỉ thị**.

---

## Sơ đồ phụ thuộc giữa các Workstream

```
WS0 (import-linter wiring)
  │
  ▼
WS1 (sửa 5 import ngược pkg/anomaly/rag → workers)
  │
  ▼
WS2 (Governance/Autonomy Control Plane — tách + khai tử hệ chết)  ◀── nền tảng, mọi WS sau phụ thuộc gián tiếp
  │
  ├──────────────┬──────────────┬──────────────┐
  ▼              ▼              ▼              ▼
WS3            WS5            WS7            WS8
(Operational   (god-object    (System Twin    (Change
 Memory)       omni_worker.py  → blast-radius) Intelligence)
                → Capability
                Registry)
                  │
                  ▼
                WS6 (Execution Plane ↔ Knowledge Plane split — topology)

WS4 (Agent Platform: trust model Ed25519 + registry + lifecycle)  ◀── song song độc lập, ưu tiên cao vì bảo mật
  │
  ▼
WS9 (Remote Agent SDK: 5 collector → OBSERVED-only, canary rollout)

WS10 (dọn manifest chết)  ◀── độc lập hoàn toàn, làm bất cứ lúc nào
```

**Sóng triển khai đề xuất** (mỗi sóng = có thể chạy song song trong sóng, tuần tự giữa các sóng):

| Sóng | Workstream | Lý do thứ tự |
|---|---|---|
| **Sóng 1** | WS0 → WS1 | Rủi ro thấp nhất, không đổi behavior, dựng lưới an toàn (lint) trước khi refactor lớn |
| **Sóng 2** | WS2 | Nền tảng — mọi quyết định mutate sau này đi qua 1 API duy nhất; phải xong trước WS5/WS6/WS7 |
| **Sóng 3** | WS3, WS4, WS10 (song song) | Độc lập với nhau, độc lập với WS5-9, có thể chạy đồng thời 3 track khác nhau |
| **Sóng 4** | WS5 → WS6 | God-object trước, tách plane sau (đúng thứ tự đã có trong Đ13 Phase B→C) |
| **Sóng 5** | WS7, WS8 (song song) | Cả 2 phụ thuộc WS2 (Autonomy Engine ổn định), độc lập với nhau |
| **Sóng 6** | WS9 | Rủi ro vận hành cao nhất (chạm VM khách hàng thật) — làm sau cùng, sau khi WS4 (trust/lifecycle) đã có nền |

---

## WS0 — Wiring `import-linter` vào CI

- **Mục tiêu:** có công cụ thực thi ranh giới dependency trước khi sửa bất kỳ import nào (để sửa xong không tái phạm).
- **Việc làm:** thêm `import-linter` vào dev-dependencies; viết `setup.cfg`/`.importlinter` khai `Contracts` ban đầu **nới lỏng** (chỉ cấm hướng rõ ràng sai: `gateway` → `workers` đã đúng sẵn, thêm luôn để khoá cứng; `pkg`/`anomaly`/`rag` → `workers` tạm thời **allowlist đúng 5 vi phạm hiện có** để CI xanh ngay, rồi xoá allowlist từng dòng ở WS1); thêm CI job chạy `lint-imports`.
- **File đụng:** `pyproject.toml` (dev dep), `.importlinter` (mới), CI workflow.
- **Test:** `lint-imports` chạy xanh với allowlist tạm; không đổi code nghiệp vụ nào.
- **Rollback:** xoá CI job, không ảnh hưởng runtime.
- **Effort:** S (nhỏ).

## WS1 — Sửa 5 import ngược `pkg/`, `anomaly/`, `rag/` → `workers/`

- **Mục tiêu:** loại bỏ circular dependency thật (Phase 1.1 của redesign doc).
- **Việc làm:** với từng file vi phạm (`pkg/reasoning/deterministic_mutate_from_evidence.py`, `pkg/reasoning/sre_output.py`, `pkg/autonomy/gate.py`, `pkg/executor/__init__.py`, `pkg/executor/mutate_governance.py`, `anomaly/sigma_calibrator.py`, `rag/redis_vector_store.py`): xác định chính xác symbol nào được import từ `workers.*`, di chuyển symbol đó xuống `pkg/` (nếu là type/interface thuần) hoặc bọc bằng dependency-injection (truyền vào qua tham số hàm/constructor thay vì import trực tiếp) nếu symbol đó thực sự thuộc orchestration layer.
- **Thứ tự sửa trong WS này:** bắt đầu từ file có ít call site nhất (khả năng cao `anomaly/sigma_calibrator.py`, `rag/redis_vector_store.py` — chỉ 1 symbol mỗi file) trước, để có early-win và pattern mẫu cho các file phức tạp hơn (`pkg/executor/mutate_governance.py`, `pkg/autonomy/gate.py` — lưu ý `gate.py` sẽ bị thay thế ở WS2 nên có thể GỘP việc sửa import của nó vào luôn WS2 thay vì sửa 2 lần).
- **Xoá allowlist** trong `.importlinter` (từ WS0) từng dòng khi mỗi file được sửa xong.
- **Test:** full suite 7348 test phải xanh sau mỗi file sửa (chạy lại toàn bộ, không chỉ file liên quan — vì đây là thay đổi import, rủi ro phá import cascade ở nơi khác).
- **Rollback:** revert từng commit riêng theo file (không gộp 5 file thành 1 commit).
- **Effort:** M.

## WS2 — Autonomy Control Plane (Governance Engine + Autonomy Engine)

Đây là workstream quan trọng nhất — nền tảng cho WS5/6/7. Dựa trực tiếp phát hiện Đ14 mục 1.

- **Mục tiêu:** (a) khai tử hoặc hợp nhất hệ `pkg/autonomy/gate.py`+`policy.py` (hệ chết, 0 call site production); (b) tách rõ 2 câu hỏi "được phép không" (Governance) vs "đủ tin cậy không" (Autonomy) thành 2 module tường minh; (c) 1 API hợp nhất `AutonomyControlPlane.decide(action, context) -> Decision{permission, authority, reason_chain}` thay cho việc 6 file tự check rải rác.
- **Quyết định kiến trúc cần chốt trước khi code** (đưa vào AskUserQuestion khi bắt đầu WS này, không tự quyết): giữ `AutonomyLevel` (FULL_AUTO/SUGGEST_ONLY/HITL/ALERT_ONLY) của hệ chết làm model chính thức và port `tier_gate.py` logic vào đó, HAY khai tử hẳn `gate.py`/`policy.py` và giữ `tier_gate.py` làm chính thức (ít việc hơn, vì `tier_gate.py` đã là hệ đang chạy thật)? **Khuyến nghị: khai tử `gate.py`/`policy.py`** — ít rủi ro hơn (không đổi hệ đang chạy đúng), chỉ cần xoá code chết + sửa lại `gateway/routes/autonomy.py` (nơi duy nhất còn dùng `policy.py`) để gọi thẳng tier/risk API thật.
- **Việc làm:**
  1. Viết `pkg/autonomy/control_plane.py` — `GovernanceEngine.check_permission()` (hợp nhất `mutate_governance.py`, không đổi logic, chỉ đổi chỗ gọi) + `AutonomyEngine.check_authority()` (hợp nhất `tier_gate.evaluate_tier_gate` + `ConfidenceLevel` + `blast_radius.assess_blast_radius` — blast-radius trở thành 1 input của authority, không phải bước riêng).
  2. `AutonomyControlPlane.decide()` gọi cả 2, trả `Decision` có `reason_chain` structured.
  3. Thay 6 điểm gọi rải rác (`evidence_mutate_emit.py`, `kafka_actions_consumer.py`, `autonomous_execute.py`, `auto_recovery_bridge.py`) bằng 1 lệnh gọi `AutonomyControlPlane.decide()` — **giữ nguyên các lớp kill-switch/nsenter/rate-limit hiện có bên ngoài `decide()`** (đây là defense-in-depth cố ý, không gộp vào 1 hàm để tránh single point of failure).
  4. Thêm CRAT event `DECISION_RENDERED` (payload = `reason_chain`) — 1 nơi trả lời "tại sao Omni quyết định X".
  5. Xoá `pkg/autonomy/gate.py`/`policy.py` (nếu khuyến nghị ở trên được duyệt) + sửa `gateway/routes/autonomy.py`.
- **Test:** viết test mới cho `AutonomyControlPlane.decide()` bao phủ đúng ma trận tier×risk×blast-radius đã có (di chuyển test case từ `test_tier_gate.py`/`test_blast_radius.py` hiện có, không viết lại từ đầu); test hồi quy toàn bộ `kafka_actions_consumer`/`autonomous_execute`/`auto_recovery_bridge` phải xanh không đổi hành vi quan sát được (chỉ đổi đường gọi nội bộ).
- **Rollback:** giữ code cũ dưới feature flag `OMNI_USE_LEGACY_AUTONOMY_CHECKS` 1 vòng deploy trước khi xoá hẳn (an toàn hơn vì đây là đường mutate — không đùa với rollback ở đây).
- **Effort:** L (lớn nhất trong toàn bộ roadmap, cần cẩn trọng nhất vì đụng đường mutate thật).

## WS3 — Operational Memory closure

- **Mục tiêu:** đóng 3 lỗ hổng Đ14 mục 5 — evidence gốc TTL-out, export không lọc trace_id, learning câm.
- **Việc làm:**
  1. **Archival Evidence Store**: bảng Postgres mới `trace_evidence_archive(trace_id, stage, content, created_at)`, ghi kèm khi `write_audit_block` chạy cho các event mang evidence gốc (không đổi CRAT schema, chỉ thêm ghi phụ cùng transaction logic ở tầng gọi).
  2. **Learning write-back**: thêm 1 dòng `write_audit_block(LEARNING_RECORDED, trace_id=...)` vào `_upsert_action_experience_on_success` (`autonomous_feedback_loop.py`) — thay đổi nhỏ, best-effort (không chặn upsert nếu ghi CRAT lỗi, theo đúng pattern try/except đã có sẵn trong hàm này).
  3. **`/compliance/export` nhận `trace_id`** (query param optional) — lọc thêm 1 điều kiện trong `_fetch_blocks`.
  4. **Endpoint mới `GET /trace/{id}/replay`** — aggregator gọi các nguồn đã có (CRAT theo trace_id, trace-stage, diag-session nếu còn TTL, brain-session nếu còn TTL, evidence archive mới nếu nguồn gốc đã hết TTL) trả về cấu trúc 7 giai đoạn.
- **Test:** unit test cho archive write, learning write-back (assert CRAT có `LEARNING_RECORDED` sau khi upsert thành công), integration test cho `/trace/{id}/replay` (dựng 1 trace giả đầy đủ 7 giai đoạn, assert response có đủ field).
- **Rollback:** toàn bộ đều additive (thêm bảng, thêm event, thêm endpoint) — không đổi hành vi cũ, rollback = revert commit, an toàn tuyệt đối.
- **Effort:** M.

## WS4 — Agent Platform maturation (ưu tiên bảo mật cao nhất)

- **Mục tiêu:** vá lỗ hổng chuỗi cung ứng thật (Đ14 mục 4) + hợp nhất registry + thêm lifecycle state.
- **Việc làm (theo thứ tự ưu tiên con):**
  1. **Trust model (ưu tiên 1 — bảo mật thật)**: tái dùng `services/audit_ledger/signer.py` (Ed25519) — `scripts/publish_agent_release.py` ký bundle bằng private key Omni; agent (`updater.py`) verify chữ ký đối chiếu public key **pinned cứng trong bundle agent** (không tải public key qua network — tránh MITM ngay chính bước lấy key). Giữ sha256 checksum hiện tại làm lớp kiểm tra toàn vẹn kênh truyền bổ sung, KHÔNG thay thế.
  2. **Registry hợp nhất**: thêm cột vào `agent_credential` hoặc bảng mới `agent_metadata(agent_id, tenant_id, last_version, last_capabilities, lifecycle_state, last_seen_at)` — Postgres backing thay vì chỉ Redis TTL=120s; Redis registry giữ vai trò "liveness cache" (đọc nhanh), Postgres là nguồn sự thật dài hạn.
  3. **Lifecycle states**: mở rộng CHECK constraint `active|revoked` → `enrolled|active|degraded|deprecated|decommissioned`; `degraded`/`deprecated` feed vào `AutonomyControlPlane` (WS2) — agent không `active` luôn hạ về HITL.
  4. **Canary upgrade**: `UPDATE_AGENT` API thêm tham số `canary_agent_id` + endpoint so sánh health trước/sau trong cửa sổ N phút trước khi cho phép gọi update hàng loạt (không tự động hoá hoàn toàn — chỉ enforce có bước quan sát, quyết định lan rộng vẫn là operator).
  5. **Version compatibility gate**: envelope thêm field `schema_version` bắt buộc; gateway reject (400, không phải chỉ log) payload dưới version tối thiểu hỗ trợ (config qua ConfigMap, có thể nới dần khi cần).
- **Test:** test ký/verify chữ ký (bundle giả lập, verify thành công/thất bại đúng); test lifecycle state ảnh hưởng tới `AutonomyControlPlane.decide()`; test gate version-compat từ chối payload cũ.
- **Rollback:** trust model là thay đổi nhạy cảm nhất về vận hành (nếu ký sai, agent thật có thể từ chối update hợp lệ) — triển khai với flag `OMNI_AGENT_REQUIRE_SIGNATURE=false` ban đầu (chỉ log cảnh báo nếu thiếu chữ ký), bật `true` sau khi verify tất cả agent lab đã có bundle ký đúng.
- **Effort:** L (đặc biệt bước 1, vì chạm production update path cho VM khách hàng thật).

## WS5 — `omni_worker.py` → Capability Registry (god-object refactor)

- Đây chính là **Phase B** đã mô tả chi tiết trong `OMNI_V2_ARCHITECTURE_REDESIGN.md` — giữ nguyên kế hoạch đó, không lặp lại ở đây. Điểm bổ sung duy nhất sau Đ14: các loop khi khởi trong Capability Registry mới nên gọi qua `AutonomyControlPlane` (WS2) thay vì gọi thẳng `tier_gate`/`mutate_governance` như hiện tại — tận dụng luôn cơ hội refactor để dọn 2 việc cùng lúc thay vì sửa lại lần 2.
- **Effort:** L.

## WS6 — Execution Plane ↔ Knowledge Plane split (topology)

- Đây là **Phase C** trong redesign doc — giữ nguyên. Phụ thuộc cứng WS2 (Decision API đã ổn định) và WS5 (registry đã tách sạch loop theo capability, dễ tách Deployment theo đúng ranh giới).
- **Effort:** L, rủi ro vận hành cao nhất về mặt hạ tầng (đổi topology K8s thật).

## WS7 — System Twin → wire vào blast-radius

- **Mục tiêu:** theo Đ14 mục 2 — World Model trở thành nguồn chính cho blast-radius, K8s live-rule làm fallback theo confidence.
- **Việc làm:**
  1. Mở rộng collector (`database.py`, `services.py`, `network.py` bên remote_agent, hoặc lớp projection `onboarding_projection.py` bên Omni) để ghi thêm predicate `depends_on`/`calls`/`serves` — bắt đầu từ **1 predicate duy nhất** (`depends_on`, dễ suy ra nhất từ connection string/service-name đã thu thập) trước khi mở rộng.
  2. `blast_radius.py`: thêm nhánh gọi `SystemModel.blast_radius()` trước, dùng kết quả nếu `revision` đủ mới + đủ dữ liệu cho resource đang xét (ngưỡng cụ thể cần định nghĩa — gợi ý: có ít nhất 1 Fact `depends_on` liên quan trong 24h gần nhất); nếu không đủ, giữ nguyên logic K8s live-rule hiện tại làm fallback — **không thay thế**, chỉ thêm nhánh ưu tiên.
  3. Wire `owned_by` (nếu có trong SystemModel) vào routing HITL notification.
- **Test:** test blast-radius với World Model có/không có dữ liệu — assert đúng nhánh được chọn; test không regression cho case hiện tại (K8s-rule vẫn đúng khi Twin chưa có dữ liệu).
- **Rollback:** additive (thêm nhánh ưu tiên), fallback giữ nguyên — an toàn.
- **Effort:** M (phần collector), S (phần wiring blast_radius).

## WS8 — Change Intelligence (bounded context mới)

- Theo thiết kế Đ14 mục 3: Postgres `change_event(tenant_id, resource_id, change_type, source, ts, detail)` có index `(resource_id, ts)`, nạp từ 3 nguồn có sẵn (discovery diff, CRAT CONFIG_CHANGED, + K8s watch informer MỚI cần viết) + tự ghi nhận hành động Execution của chính Omni.
- **Việc làm theo thứ tự**: (1) bảng Postgres + migration; (2) ETL nạp từ 2 nguồn có sẵn trước (discovery diff, CRAT) — không cần viết mới, chỉ cần subscribe; (3) `get_changes_before(resource_id, incident_ts, window)` API; (4) inject `recent_changes` vào `AnalystAdvisory` prompt (Reasoning context); (5) K8s watch informer (việc mới hoàn toàn, phức tạp nhất, có thể để làm sau nếu muốn giá trị nhanh từ 2 nguồn đầu trước).
- **Test:** test ETL idempotent (không duplicate khi replay); test `get_changes_before` trả đúng thứ tự thời gian; test prompt có field mới không phá cấu trúc AnalystAdvisory hiện tại.
- **Rollback:** hoàn toàn additive, bảng mới không ảnh hưởng luồng cũ; risk duy nhất là nếu `recent_changes` làm prompt LLM dài quá ngân sách context (`OMNI_LLM_NUM_CTX`) — cần giới hạn số lượng change trả về (ví dụ top 10 gần nhất) ngay từ đầu.
- **Effort:** M-L tuỳ có làm K8s watcher hay không (có thể chia thành 2 lô nhỏ).

## WS9 — Remote Agent SDK: chuẩn hoá 5 collector còn lại về OBSERVED-only

- Đây là **Phase D** trong redesign doc — giữ nguyên kế hoạch canary (sửa `storage/logs/database/services/network` theo mẫu `system.py`, ngưỡng do server đẩy xuống qua `register`). Bổ sung sau Đ14: nên làm SAU WS4 (đã có canary-upgrade + version-compat gate) để tận dụng đúng cơ chế rollout dần đã xây, không phải tự chế lại quy trình canary riêng cho lần này.
- **Effort:** L, rủi ro vận hành cao nhất (VM khách hàng thật).

## WS10 — Dọn manifest chết

- Xoá/annotate `k8s/services/omni-analyst-service.yaml` dangling reference trong Makefile, làm rõ trạng thái các manifest `-production` chưa từng deploy. Độc lập hoàn toàn, làm bất cứ lúc nào, effort S.

---

## Bảng tổng hợp effort & rủi ro

| WS | Nội dung | Effort | Rủi ro vận hành | Phụ thuộc |
|---|---|---|---|---|
| WS0 | import-linter wiring | S | Không | — |
| WS1 | Sửa 5 import ngược | M | Thấp | WS0 |
| WS2 | Autonomy Control Plane | **L** | Trung bình-cao (đường mutate) | WS1 |
| WS3 | Operational Memory closure | M | Thấp (additive) | — |
| WS4 | Agent Platform + trust model | **L** | Cao (VM khách hàng) | — |
| WS5 | `omni_worker.py` → registry | L | Trung bình | WS2 |
| WS6 | Execution/Knowledge plane split | L | Cao (đổi topology K8s) | WS2, WS5 |
| WS7 | System Twin → blast-radius | M | Thấp (additive, fallback giữ nguyên) | WS2 |
| WS8 | Change Intelligence | M-L | Thấp (additive) | — |
| WS9 | Remote Agent SDK chuẩn hoá | **L** | Cao nhất (VM khách hàng, canary) | WS4 |
| WS10 | Dọn manifest chết | S | Không | — |

## Điểm bắt đầu đề xuất

**Sóng 1 (WS0 → WS1)** — rủi ro thấp nhất, không đổi hành vi, dựng lưới an toàn trước khi đụng bất cứ refactor lớn nào. Đây là điểm khởi động hợp lý nhất để bắt đầu ngay khi được chỉ thị triển khai.

Chưa bắt đầu code bất kỳ WS nào trong lượt này — tài liệu này là kế hoạch chờ duyệt, đúng quy trình dự án (PLAN trước, GIT/code chỉ khi được chỉ thị).
