# Current Session Handoff

## Deliverable hiện tại
**Provider Portal UI gap-fill (4 gap): drift/runtime trên `/agents`, `/settings`
enrollment/credential admin, `/audit` CRAT hash-chain, diagram history/diff trên
`/understanding` — CODE + build + test DONE, chưa commit.**
- Sprint "Nhân viên SRE" IT-1..4 DONE (commit `e64e338`/`cebd84b`/`a364fc2`/`8fc5aa3`, pushed) —
  đừng re-verify.
- `aoip-agent.service` trên `cust-app` chạy employee từ `2026-07-08 14:25:56 +07`. Mốc so Twin
  24h là **~2026-07-09 14:23 +07** — CHƯA tới, đừng kết luận sớm (việc UI này chạy song song
  trong lúc chờ mốc đó).

## Đã hoàn thành trong phiên (audit gap → 4 gap → implement cả 4)
Audit ban đầu (agent Explore) so backend/Productization Iter 1-26 + sprint IT-1..4 với route
thật trên `ui/apps/provider-portal` → 5 gap, làm 4/5 (bỏ `/missions` vì chưa xác nhận backend).

1. **Gap 1 — Agent drift + runtime trên `/agents`**: `src/aoip/console/agents.py` đọc thêm
   `omni:agent:release_manifest`, tính `drift_status`/`runtime` (mirror `_classify_drift` ở
   `gateway/routes/agent_commands.py`, KHÔNG import chéo — console BFF tự đọc Redis riêng).
   `AgentsTable.tsx` thêm cột Runtime/Drift; `page.tsx` thêm tile "Fleet drifted". CSS pill mới
   `.current/.drifted/.unknown`. Test: `tests/test_aoip_provider_agents.py` (+1 test mới).

2. **Gap 2 — Settings (enroll-token issue + credential revoke)**: `src/aoip/console/settings.py`
   (mới) gọi thẳng `AdminConfigRepo` (IT-3 store) qua `request.app.state.pool` — KHÔNG proxy qua
   gateway. 3 endpoint mới trong `app.py` (`GET /settings`, `POST .../enroll-tokens`,
   `DELETE .../agent-credentials/{tenant}/{agent}`), gate bằng `P_CHANGE_POLICY`.
   **Gotcha kiến trúc**: `nav.ts` có GOVERNING RULE cấm "policy-editor" domain trên portal — đã
   hỏi user, được duyệt thêm NGOẠI LỆ ghi rõ trong comment (enrollment là vận hành thiết yếu,
   RBAC + audit đầy đủ, không phải billing/CRM). `/settings` cũ thực ra **crash** (route không
   có trong `PROVIDER_NAV`, `find()!` trả `undefined`) — không phải "stub đang chờ", là orphan.
   UI: `lib/settings.ts`, `app/settings/SettingsPanel.tsx` (client, issue+revoke), `page.tsx`.
   Test: `tests/test_aoip_provider_settings.py` (4 test, fake asyncpg pool riêng).

3. **Gap 3 — `/audit` chiếu CRAT hash-chain thật**: `src/aoip/console/audit.py` (mới) đọc
   `audit_chain:blocks` (default) + `audit_chain:*:blocks` (named tenant) — nguồn
   `services.audit_ledger.chain_writer`, KHÔNG source thứ hai. Gate `P_RAW_EVIDENCE` (audit là
   evidence nhạy cảm, không phải mọi provider viewer role). Phân biệt với `/incidents` (SIEM-only
   verdict view) — `/audit` là toàn bộ event type (ADVISORY_DECISION/MUTATION_TRAPPED/...).
   Test: `tests/test_aoip_provider_audit.py` (3 test).

4. **Gap 4 — Diagram History/Diff panel trên `/understanding`**: port từ `ui/app` cũ (Iter 23,
   Tailwind) sang provider-portal (aoip-* CSS). `lib/diagram-diff.ts` (LCS diff, pure), route
   proxy `app/api/onboarding/diagram-history/route.ts` (server-side gateway call — cần vì panel
   client-side interactive, `lib/gateway.ts` chỉ dùng được ở server component), component
   `DiagramHistoryPanel.tsx`. **Gotcha lint**: `react-hooks/set-state-in-effect` chặn setState
   đồng bộ ngay đầu effect body — phải viết IIFE + cancelled-guard (mirror
   `components/mermaid-diagram.tsx`), KHÔNG gọi thẳng hàm `load()` có `setLoading` ở đầu.

## Verify đã chạy
- `.venv/bin/python -m pytest tests/ -q -k "aoip_provider or console or agent_enrollment or
  onboarding" --ignore=tests/integration --ignore=tests/e2e_portals` → 110 passed.
- `npx tsc --noEmit -p apps/provider-portal/tsconfig.json` → sạch.
- `npx next build` (trong `ui/apps/provider-portal`) → sạch, cả 4 route mới (`/agents` cột mới,
  `/settings`, `/audit`, `/understanding` panel) build thành công.
- **CHƯA chạy** `make e2e-portal` (Playwright thật trên pod) — nên chạy trước khi coi UI work là
  production-verified, không chỉ build-verified.

## Next step chính xác
1. (Nếu user yêu cầu) commit + push toàn bộ UI gap-fill này — 1 hoặc nhiều commit theo gap.
2. Chạy `make e2e-portal` để verify thật trên pod (build/tsc chỉ chứng minh compile, không chứng
   minh chạy đúng — theo bài học Iteration 1 "test pass + push KHÔNG chứng minh đã deploy").
3. Deploy `omni-gateway`/`aoip-provider-portal` nếu muốn thấy thay đổi trên môi trường lab thật.
4. Sau đó quay lại theo dõi mốc Twin 24h cho IT-4 (~2026-07-09 14:23 +07) — xem
   `omni:aoip:system_model:staging-sim` cho `cust-app`.
5. Gap còn lại chưa làm: `/missions` (SectionStub) — cần xác nhận có backend API đứng sau chưa
   trước khi động vào (không bịa UI cho capability chưa tồn tại).

## Không được làm lại
- Đừng re-verify IT-1..IT-4 (sprint Nhân viên SRE) — đã DONE, đã push.
- Đừng gỡ NGOẠI LỆ Settings khỏi `nav.ts` — đã được user duyệt tường minh trong phiên này.
- Đừng đổi `/audit` thành duplicate của `/incidents` — hai trang có nguồn/phạm vi khác nhau
  (toàn bộ CRAT chain vs SIEM-only verdict).

## Tài liệu liên quan
- Memory: sẽ ghi `project_provider_portal_ui_gapfill_2026_07_08` sau khi checkpoint xong.
- `docs/plans/aoip-provider-portal-slices.md` — governing rule gốc (đã có ngoại lệ mới trong
  `ui/apps/provider-portal/lib/nav.ts`, chưa đồng bộ ngược vào doc này — có thể cần làm ở lần sau).

## Blockers
Không có.
