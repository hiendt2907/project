# Omni — session memory pointer

Đọc file này cùng `AGENTS.md` trước mọi task.

- Bản đồ codebase và trạng thái verify: `docs/CODEBASE.md`
- Handoff tiếp phiên: `docs/handoffs/CURRENT_SESSION.md`
- **Sổ đo chất lượng + đánh giá khả thi (đối chiếu qua mọi phiên):**
  `docs/measurement/OMNI_QUALITY_BASELINE.md` — baseline advisory đo được, kiến trúc đo 5 tầng,
  kế hoạch giả lập khách hàng thật. **Đọc trước khi đưa ra bất kỳ tuyên bố nào về chất lượng
  hay tính khả thi**; thêm phép đo mới vào đây, không rải ra handoff.
- Báo cáo verify đầy đủ: `docs/reports/frontend-backend-logic-verification-2026-07-14.md`
- Quyết định tách execution engine/control plane: `docs/architecture/ADR-004-runtime-convergence.md`
- Chỉ mục tài liệu: `docs/DOCUMENTATION_INDEX.md`

Checkpoint 2026-07-14: backend `6150 passed`, boundary/safety `61 passed`, portal E2E
`18/18`, pre-deploy `17/17`, portal builds/typechecks pass, production npm audit có
0 high-severity vulnerabilities. `src/workers/` tiếp tục là execution engine;
`src/aoip/` là product/domain/control-plane; không gộp vật lý. Khối verify này đã
được commit + push lên `main` ngày 2026-07-15 (control-plane `b6941d5`, portal
`362b7cd`, docs kèm theo).
