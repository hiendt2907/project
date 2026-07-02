# Product Proof Row Template

Dùng khi thêm/cập nhật một dòng trong bảng "Capability Matrix" của `docs/product/PRODUCT_PROOF.md`:

```
| <Capability name> | <Code: ✅/❌> | <Deployed: ✅/❌/⚠️> | <Runtime verified: ✅/❌/⚠️> | <Operator-visible: ✅/❌/⚠️> | <Evidence — lệnh/API/log/query thật đã chạy, kèm output cụ thể> |
```

Quy tắc:
- Cột "Evidence" KHÔNG được là mô tả chung chung ("đã test", "hoạt động tốt") — phải là lệnh/API
  call/log line/Redis key thật kèm kết quả.
- ⚠️ dùng khi PARTIAL — luôn giải thích phần nào thiếu ngay trong ô Evidence hoặc ghi chú kèm.
- Nếu capability liên quan tới golden journey, cập nhật luôn mục "Golden Journey" bên dưới bảng nếu
  trạng thái tổng thể thay đổi (vd 2/3 host → 3/3 host).
