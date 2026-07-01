---
description: Chuẩn bị an toàn trước khi /clear — cập nhật handoff và chốt trạng thái repo.
---

Bạn đang chuẩn bị kết thúc phiên để người dùng chạy `/clear`. Thực hiện tuần tự:

1. **Dừng mọi implementation mới.** Không bắt đầu tính năng hay refactor nào nữa.
2. **Kiểm tra Git state:** chạy `git status --short`, `git branch --show-current`, `git log --oneline -5`.
3. **Cập nhật `docs/handoffs/CURRENT_SESSION.md`** theo `docs/handoffs/TEMPLATE.md`:
   - Deliverable, Definition of Done, Trạng thái, Đã hoàn thành.
   - Branch + commit + Working tree **đúng tại thời điểm này**.
   - **Next step chính xác** = một hành động chạy được ngay.
   - Lệnh cần chạy lại; Không được làm lại; Blockers.
   - Ngắn gọn, không kể lại toàn bộ lịch sử, không chứa secret/token/customer data.
4. **Cập nhật artifact bị ảnh hưởng** nếu implementation đụng tới: roadmap, ledger, CHANGELOG, MEMORY.md, docs liên quan.
5. **Chạy verification tối thiểu cần thiết** (ví dụ `bash tests/claude_hooks/test_session_hooks.sh` hoặc test liên quan tới thay đổi). Báo kết quả.
6. **Không tự commit** trừ khi người dùng đã cho phép rõ ràng. Nếu chưa, chỉ báo là handoff đã sẵn sàng để commit.
7. **Báo cáo checkpoint (≤ 20 dòng):**
   - Handoff đã sẵn sàng (đường dẫn).
   - Branch, HEAD commit, tóm tắt working tree.
   - Câu lệnh nên chạy đầu tiên sau khi `/clear`.
8. Kết thúc bằng một câu: người dùng có thể chạy `/clear` một cách an toàn.

Lưu ý: command này **không** tự chạy `/clear` — Claude Code không hỗ trợ tự trigger. Việc `/clear` do người dùng thực hiện thủ công.
