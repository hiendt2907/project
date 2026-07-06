# ADR-003 — Backend-Frontend Parity (Operator Visibility)

- **Status**: Accepted (2026-07-06, Iteration 25)
- **Context**: Nhiều capability backend đã VERIFIED_RUNTIME nhưng chỉ quan sát được qua
  `redis-cli`/`psql`/curl nội bộ (cột Operator-visible ❌ trong `PRODUCT_PROOF.md`). Điều này vi
  phạm tinh thần Golden Journey ("toàn bộ phải chạy qua official API/portal") và làm sản phẩm
  không demo/pilot được cho người không phải developer.

## Decision

1. **Parity rule**: Mọi capability backend đạt VERIFIED_RUNTIME và nằm trên Golden Journey phải
   trở thành operator-visible trên portal — không nghiệm thu capability chỉ bằng datastore query.
2. **Hiển thị cho người không hiểu hệ thống**: nhãn ngôn ngữ đời thường (theo ngôn ngữ portal
   hiện hành) + câu giải thích ngắn về ý nghĩa/hệ quả; trạng thái dùng badge/màu ngữ nghĩa;
   KHÔNG hiển thị raw Redis key, mã trạng thái nội bộ trần trụi, hay JSON thô làm UI chính.
   Empty-state phải nói rõ "vì sao trống và cần làm gì tiếp".
3. **Thang đo Operator-visible** trong capability matrix của `PRODUCT_PROOF.md`:
   - ❌ không có UI (chỉ datastore/curl nội bộ)
   - ⚠️ có UI nhưng phải hiểu hệ thống mới đọc được (raw key/state code/JSON)
   - ✅ người ngoài đọc hiểu được (persona test: chỉ đọc trang, trả lời được "hệ thống đang
     biết gì / thiếu gì / cần tôi làm gì")
4. **Nhịp thực hiện**: mỗi iteration nâng đúng MỘT capability ❌/⚠️ → ✅ (không mở nhiều mặt
   trận), ưu tiên theo giá trị Golden Journey. E2E của slice phải assert nội dung hiển thị
   (nhãn/badge/empty-state), không chỉ HTTP 200.

## Consequences

- `PRODUCT_CONTRACT.md` thêm §10 tham chiếu ADR này; capability matrix chuyển cột
  Operator-visible sang thang 3 mức.
- Giữ nguyên các ràng buộc hiện có: honest-error (không mock fallback), portal không hiển thị
  thứ backend chưa enforce (§9), không mở remediation/billing/multi-region.
