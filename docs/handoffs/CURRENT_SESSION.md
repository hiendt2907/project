# Current Session Handoff

## Deliverable hiện tại
**Iteration 18 — Phase-1 Product & Architecture Contract Freeze: DONE (VERIFIED_RUNTIME).**
Đây là bước mở màn của Production Productization Plan (master plan do user cung cấp 2026-07-03,
mapping: Phase 0 = đóng iter 17 ✅ đã đóng từ trước; Phase 1 = contract freeze ✅ iteration này).

## Definition of Done
Phase 1 theo master plan: Product Contract canonical + ADR command protocol + shared protocol
source-of-truth + production hygiene, tất cả có test + runtime proof. **DONE.**

## Đã hoàn thành
- `docs/product/PRODUCT_CONTRACT.md` (mới): target customer, supported platforms, Golden Journey
  chính thức, catalog đúng 3 remediation đầu, 5 hard-zero SLO, tier gates, data boundary,
  non-goals, pilot acceptance criteria. Mọi feature mới phải map vào một bước Golden Journey.
- `docs/architecture/ADR-002-command-protocol.md` (mới): canonical command protocol = HTTP contract
  + state machine của `gateway/routes/agent_runtime.py`. **Phát hiện quan trọng**: hướng ADR-001 §5
  (gateway import `DurableCommandChannel`) là SAI CHIỀU — bản gateway đã vượt bản aoip về an toàn
  (atomic Lua claim, fencing token, heartbeat, record_version); `DurableCommandChannel` thiếu
  fencing, chỉ còn dùng test/demo → legacy có sunset criteria (Phase-3 durable Control Plane).
  ADR-001 đã được chú thích superseded ở §5.
- `src/aoip/protocol/__init__.py` (mới): nguồn chân lý duy nhất cho command state vocabulary
  (9 states, TERMINAL/PROGRESS, `is_legal_transition()`, PROTOCOL_VERSION=1). `agent_runtime.py`
  và `delivery.py` import chung (refactor import-only, hành vi không đổi).
- `tests/test_aoip_protocol_contract.py` (mới, 13 test): parse Lua `_CLAIM_SCRIPT` đối chiếu bảng
  TERMINAL với protocol (chặn drift bản chép thứ 3 không import được), + transition invariants.
- `requirements.lock` (mới): pip freeze Python 3.13.5. Dockerfile CHƯA wire lock — ghi
  `TECH_DEBT_BACKLOG.md` #13, là slice riêng.

## Verification đã chạy
- Full suite: `pytest tests/ -q --ignore=tests/integration` → **5965 passed, 0 failed**.
- Runtime proof: rebuild `omni-gateway:latest` (`f9ccdf1fe277…`) + `multi-agent-system:latest`
  (`bfa8fe4b053f…`); rollout restart omni-gateway/omni-fullstack/omni-onboarding, tất cả OK.
  `kubectl exec`: gateway `agent_runtime.TERMINAL is protocol.TERMINAL_STATES == True`; fullstack
  `delivery.TERMINAL_STATES is protocol.TERMINAL_STATES == True`. `/readyz` → 200 redis+postgres ok.
  `OMNI_AUTO_EXECUTE_ENABLED=false` reconfirmed.

## Branch và commit
`main`. Commit của iteration này: xem `git log` (nhóm: protocol code+test, docs contract/ADR,
governance backfill).

## Quyết định đã chốt (KHÔNG re-litigate)
- Canonical command protocol = `agent_runtime.py` semantics; `DurableCommandChannel` = legacy,
  không thêm feature, sunset cùng Phase-3.
- Không tạo package top-level `aoip_protocol/` riêng — dùng `src/aoip/protocol/` (cả 2 image đã
  COPY `src/aoip/`).
- 3 remediation đầu cố định theo PRODUCT_CONTRACT §4; không mở action song song.
- Kill-switch mặc định false vĩnh viễn; tier gates theo PRODUCT_CONTRACT §6.

## Blockers
None.

## Next step chính xác
**Phase 2 — Golden Journey Read-only** (master plan §7): hành trình create-tenant → export-audit
qua official API/portal, không Redis/DB manual. Ứng viên slice đầu: operator portal UI cho
competency/unknowns/diagram (hiện API-only — carry-over iteration 17). Trước khi bắt đầu, đọc
`PRODUCT_CONTRACT.md` §3 + `PRODUCT_PROOF.md` Iteration 18 + `ADR-002`.

## Không được làm lại
- Không quay lại hướng "gateway import DurableCommandChannel" (ADR-002 đã loại).
- Không chép tay state constants ở chỗ mới — import `aoip.protocol`.
- Không mở billing/multi-region/action mới song song (PRODUCT_CONTRACT §9).
- Không wire requirements.lock vào Dockerfile mà không có full runtime verify (slice riêng).

## Lệnh cần chạy lại
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` trước khi bắt đầu Phase 2.

## Tài liệu liên quan
- `docs/product/PRODUCT_CONTRACT.md` · `docs/architecture/ADR-002-command-protocol.md`
- `docs/product/PRODUCT_PROOF.md` (Iteration 18) · `docs/architecture/TECH_DEBT_BACKLOG.md`
- `docs/operations/AUTONOMOUS_LOOP_LEDGER.md` / `AUTONOMOUS_LOOP_STATE.json`
