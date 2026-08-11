# Talking points — buổi cafe demo với CTO (Đ55)

Tài liệu đi kèm `cafe_demo_preflight.sh` + `cafe_demo_payment_api.sh`. Không phải slide — đây là
những câu trả lời đã kiểm chứng bằng code/log thật, dùng khi CTO hỏi trực tiếp.

## 1. "Lỡ nó làm sai thì sao?" — câu chuyện "cấm ở mức nào"

**Sự thật hiện tại (đã tra code, không suy đoán):** làn VM/khách hàng (nơi agent chạy trên máy
CTO) chỉ có **ĐÚNG 3 lệnh** được phép tự động — `systemd.restart_unit`, `systemd.reset_failed`,
`systemd.journal_vacuum` (`_SUPPORTED_CAPABILITIES` trong `auto_recovery_bridge.py`). Cả 3 đều xếp
hạng rủi ro **LOW** (`risk_taxonomy.py`). Không có lệnh xoá, không có lệnh sửa cấu hình, không có
lệnh chạm database.

**Trả lời trực tiếp, không né:** "Hiện tại chưa có ví dụ MEDIUM/HIGH nào để em demo cảnh bị chặn
trên đúng làn VM — vì đơn giản là chưa có lệnh nào ở mức đó tồn tại để mà chặn. Đây là thiết kế
cố ý: bắt đầu hẹp nhất có thể (chỉ restart/reset dịch vụ), không phải thiếu sót do quên làm."

**3 lớp chặn đã có thật, verify được ngay tại demo (không phải lời hứa suông):**

1. **Danh sách lệnh đóng cứng trong code** — không phải AI "tự kiềm chế", mà `capability` không có
   trong 3 cái tên đó thì code từ chối trước khi chạm tới AI. Cho CTO xem thẳng đoạn
   `_SUPPORTED_CAPABILITIES = frozenset({...})` trong `auto_recovery_bridge.py`.
2. **Tier gate** (vừa demo sống) — dù capability hợp lệ, tier `shadow` vẫn chặn tự thực thi
   (`SUGGEST` only). Đổi tier tenant sang `shadow` và chạy lại drill là cách chứng minh trực tiếp
   nhất, không cần nói suông.
3. **Ngưỡng tin cậy (confidence gate)** — đã xảy ra thật trong lúc chuẩn bị demo hôm nay: một gợi ý
   sửa lỗi với độ tin cậy 0.71 bị **từ chối tự thực thi** vì dưới ngưỡng 0.75, dù hệ thống ĐÃ tìm ra
   cách sửa. "Tìm ra cách sửa" và "được phép tự làm" là hai việc khác nhau — hệ thống không tự tin
   cho tới khi tích luỹ đủ kinh nghiệm THẬT của chính khách hàng đó (không mượn kinh nghiệm tenant
   khác — đây là fix vừa vá hôm nay).

**Nếu CTO hỏi "vậy khi nào có thêm lệnh rủi ro cao hơn":** trả lời thật — "mở rộng dần theo đúng
mô hình thử việc: bắt đầu ở `shadow` (chỉ quan sát), lên `assist` khi tin cậy đủ để tự làm việc rủi
ro thấp, `auto` khi đã chứng minh qua nhiều sự cố thật. Anh là người quyết định tenant của mình đi
tới đâu, không phải mặc định bật hết."

## 2. "Nó thật sự hiểu hệ thống của em không?"

**Có bằng chứng thật, không phải mô tả suông** — System Twin (`omni:aoip:system_model:{tenant}`)
tự học qua discovery scan định kỳ, ví dụ thật quan sát được hôm nay cho tenant demo:
`host:cust-app runs_service rpcbind`, `host:cust-db connects_to host:cust-app` — có confidence
score + provenance trỏ về đúng trace nào tạo ra fact đó.

**Giới hạn thật, nói thẳng nếu bị hỏi sâu:** Twin hiện chưa gắn được tên cụ thể một số service ứng
dụng (thấy process `python3` chung chung thay vì tên service riêng) — đây là hạn chế thật của cơ
chế phát hiện tự động, chưa fix, không nên giấu nếu CTO hỏi kỹ.

## 3. Storyboard video ngắn (gửi trước buổi cafe, 2-3 phút)

Quay màn hình + giọng nói, dùng LẠI chính luồng đã verify sống hôm nay — không cần dàn dựng gì
mới. Cắt cảnh theo timeline dưới, mỗi đoạn 20-30s:

| # | Thời lượng | Hình | Lời thoại gợi ý |
|---|---|---|---|
| 1 | 0:00-0:20 | Terminal: `systemctl status payment-api` (đang chạy khoẻ) | "Đây là 1 trong nhiều server mô phỏng hạ tầng khách hàng — không có ai canh 24/7." |
| 2 | 0:20-0:35 | Gõ lệnh dừng service thật | "Bây giờ mình giả lập sự cố lúc 3h sáng — dừng thật service này." |
| 3 | 0:35-1:00 | Điện thoại: tin Telegram đầu tiên hiện lên (thẻ chẩn đoán) | "20 giây sau, hệ thống tự phát hiện, tự chẩn đoán — không ai bấm gì." |
| 4 | 1:00-1:30 | Điện thoại: tin outcome ✅ hiện lên | "Vài phút sau, nó tự sửa xong, báo kết quả riêng — có audit trail đầy đủ, không phải hộp đen." |
| 5 | 1:30-1:50 | Terminal: `systemctl status` xác nhận active lại | "Kiểm tra độc lập trên chính server — không tin lời hệ thống tự báo, tự verify lại." |
| 6 | 1:50-2:15 | Terminal: xem entry audit chain / hoặc nói bằng lời | "Mọi quyết định ghi vào sổ không sửa được — kiểu blockchain nội bộ, để không phải chỉ tin AI nói suông." |
| 7 | 2:15-2:30 | Cắt cảnh nói chuyện trực tiếp (mặt) | "Đây là bản demo trên hạ tầng lab — muốn nói chuyện kỹ hơn về hệ thống thật của anh, mình gặp cafe nhé." |

**Ghi chú kỹ thuật khi quay:** dùng đúng `cafe_demo_payment_api.sh` (đã dry-run thật, không đứng
hình) — chạy `cafe_demo_preflight.sh` ngay trước khi quay để chắc cooldown sạch, agent healthy.
