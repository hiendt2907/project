# Current Session Handoff

## Deliverable hiện tại
**Hoàn tất sản phẩm theo sprint "Nhân viên SRE": IT-4 ĐÓNG (2026-07-13) → đang làm IT-5
(safe update/rollback qua command channel).**

## IT-4 — ĐÓNG, đừng re-verify
- Bằng chứng soak: employee chạy liên tục `2026-07-09 07:05:39 → 2026-07-10 16:25:46 +07`
  (~33h > 24h), dừng do VM shutdown chủ động; Twin staging-sim đo 2026-07-13 10:00 tươi,
  93 facts / 25 fact cust-app, không mất so baseline 76. Rollback drill 2 chiều PASS từ trước.
- Đã ghi VERIFIED_SOAK vào `docs/product/PRODUCT_PROOF.md` (Iteration 30, cuối section).

## IT-5 — scope (theo docs/plans/sprint-agent-sre-employee-production.md)
- Update = durable command trên AOIP daemon: download → verify sha256 vs release manifest
  (IT-2, `make publish-agent-release`) → swap → health-check window → fail thì tự rollback
  về bundle N-1 giữ trên VM.
- Outcome `updated/rolled_back` + version báo về command channel, ghi CRAT event.
- DoD: trên cust-app (a) update thành công lên version mới; (b) cố ý ship bundle hỏng →
  health-gate fail → tự rollback, Omni nhận outcome `rolled_back`. Sau đó migrate nốt
  cust-edge/cust-db sang AOIP daemon bằng chính cơ chế update này (fleet 3/3 employee).
- Nền có sẵn: `remote_agent/updater.py` (download+sha256+extract+restart, KHÔNG health-gate),
  durable inbox/lease/fencing/idempotency ở `aoip/agent/` (`daemon.py`, `delivery_loop.py`,
  `inbox.py`), release manifest 2 hash (IT-2/IT-4).
- Gotcha ship bundle: `COPYFILE_DISABLE=1` khi tar trên macOS (AppleDouble phá hash).
- Feature mới viết ở `src/aoip/agent/` (ADR-001); `remote_agent/` chỉ compat tối thiểu.

## Sau IT-5 (kế hoạch đã duyệt với user 2026-07-13)
IT-6 (command outcome durability PG + chaos proof) → IT-7 (soak + offline recovery + sprint
review, điền cột SAU 6 metric) → IT-8 stretch (mission skeleton) → `/missions` portal +
Playwright cho 4 trang portal mới → quyết định xoá root `ui/` (cần user).

## Không được làm lại
- IT-1..IT-4 DONE (commit e64e338/cebd84b/a364fc2/8fc5aa3 + đóng sổ phiên này).
- 4 gap UI provider portal DONE, user đã click-through verify (36ee34d).

## Ghi chú phụ
- `.claude/launch.json` (dev tooling, untracked) — 4 config dev server, không đụng code sản phẩm.
