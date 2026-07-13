# Current Session Handoff

## Deliverable hiện tại
**Provider portal dễ hiểu cho người không kỹ thuật — đợt 1 DONE (commit `c27c86a`, deployed,
E2E 18/18 xanh trên cluster).** User đã đổi ưu tiên giữa phiên (2026-07-13): "mọi thứ rõ ràng
chính xác trên UI, người không rành kỹ thuật vẫn hiểu, tất cả backend phải hiển thị, tập trung
provider trước" — sprint IT-5 backend TẠM DỪNG (xem dưới).

## Đã làm phiên này
1. **IT-4 ĐÓNG** (commit `fe4d7a7`): soak 33h liên tục (2026-07-09 07:05→07-10 16:25 +07),
   Twin 93 facts không mất so baseline 76 — VERIFIED_SOAK ghi vào PRODUCT_PROOF Iteration 30.
2. **Portal đợt 1** (commit `c27c86a`, image aoip-provider-web:latest rebuilt + rollout,
   `make e2e-portal` 18 passed):
   - `components/PageIntro.tsx` — mô tả đời thường + chú giải thuật ngữ, đã gắn vào
     Overview/Agents/Understanding/Human-Inbox/Incidents/Settings/Audit.
   - Nav 12 mục nhãn tiếng Việt (`lib/nav.ts`, GOVERNING RULE giữ nguyên + comment cập nhật).
   - Trang mới: `/pipeline` (+`/pipeline/[traceId]` — 12 bước STAGE_VI khớp
     `pkg/observability/pipeline_stages.py`, đèn ok/fail/skip/pending), `/kpi`
     (gateway `/kpi/summary|trend`), `/operations`, `/tenants` (console BFF có sẵn).
   - `lib/pipeline.ts` chứa STAGE_VI/LANE_VI — bản dịch dùng chung, đổi stage backend PHẢI sửa đây.
   - E2E spec `tests/e2e_portals/specs/provider_overview.spec.ts`: sửa test nav count 7→12
     (stale từ trước), thêm 5 test mới (PageIntro/pipeline/kpi/operations+tenants).

3. **Fix hiểu nhầm /pipeline** (commit `dd9f9c9`, deployed, E2E 18/18): user thấy discovery
   traces "đứng hàng loạt" — không phải bug backend; ONBOARDING_DISCOVERY chỉ có 1 bước
   EVIDENCE theo INV_KNOWLEDGE_NOT_ALERT. UI nay tách khu "Sự cố" (12 bước, chỉ 4 lane
   chẩn đoán qua `isDiagnosticLane()`) vs "Tín hiệu học hỏi" (✓ đã ghi nhận, hoàn thành);
   chi tiết lane học hỏi hiện card giải thích thay vì 12 bước.

## Gap portal còn lại (đợt 2 — theo audit Explore agent phiên này)
- Drill-down sự cố dùng console `/incident/{tenant}/{cid}` (đã có endpoint, chưa có trang).
- Advisory/brain card ngôn ngữ tự nhiên (gateway `/trace/{id}/advisory|brain`).
- `/support-access/{tenant}` (lịch sử phiên support) chưa lên UI.
- Platform/Worker health (port `/workers` app cũ), RAG/KB stats (gateway `/kb`).
- Việt hoá nốt nhãn bảng trong `/understanding` (Competency/Facts headers còn Anh),
  `/incidents` MetricStat labels còn Anh.
- Tenant portal chưa đụng (user nói provider trước).

## IT-5 TẠM DỪNG — code lõi ĐÃ VIẾT, chưa test, đang trong working tree (KHÔNG commit)
Files: `src/aoip/agent/updater.py` (mới — apply_update/startup_gate/make_update_executor/
make_update_reconciler), `src/aoip/agent/omni_client.py` (+download_release_bundle),
`src/aoip/agent/daemon.py` (+reconciler param), `src/aoip/agent/employee.py` (startup_gate
trong main + wire update executor/reconciler), `src/gateway/routes/agent_commands.py`
(+GET /webhook/agent/release/bundle, Redis `omni:agent:release_bundle` base64),
`scripts/publish_agent_release.py` (build tar deterministic + release_tar_sha256),
`Makefile` (publish-agent-release đẩy cả bundle), `scripts/aoip-agent-guard.sh` (mới —
crash-loop guard ExecStartPre), `scripts/aoip-agent.service` (+ExecStartPre).
Thiết kế: update = durable command verb UPDATE_AGENT; agent tải bundle TỪ GATEWAY (không URL
ngoài); executor block-forever chờ restart chết giữa RUNNING (chủ ý); health-gate ở
startup_gate boot mới (self-hash vs expected); rollback N-1 `/var/lib/aoip/releases/`;
bundle hỏng không boot nổi Python → guard shell restore sau 3 boot; reconciler đọc result
marker báo outcome đúng 1 lần.
**Còn thiếu khi resume IT-5**: tests (updater unit + gateway route + guard shell qua bash),
pytest full, deploy gateway, VM drill (a) update thành công (b) bundle hỏng tự rollback,
migrate cust-edge/cust-db, PRODUCT_PROOF.

## Không được làm lại
- IT-1..IT-4 DONE + đóng sổ (`fe4d7a7`). Portal đợt 1 DONE (`c27c86a`), 18/18 E2E xanh.
- Đừng re-audit portal — bảng gap ở trên là kết quả audit 2026-07-13 rồi.

## Next step chính xác
1. Portal đợt 2 theo bảng gap trên (ưu tiên: incident drill-down + advisory card VI).
2. Khi user cho quay lại backend: resume IT-5 theo mục "Còn thiếu" ở trên.
