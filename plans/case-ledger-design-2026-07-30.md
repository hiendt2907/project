# Sổ ca (Case Ledger) — thiết kế

> Nguồn sự thật duy nhất để đánh giá năng lực Omni. Mọi thứ khác đọc từ đây:
> tốt nghiệp playbook, biên bản hết thử việc, Omni tự xin quyền, chống bùa số.

## Vì sao cần

Bốn năng lực đã thống nhất — trí nhớ (lần 2 khác lần 1), chính kiến (từ chối kèm
bằng chứng), tham vọng (tự xin mở rộng quyền), và khả năng chứng minh cho admin
khách — đều cần **cùng một tập dữ liệu chưa tồn tại**: một bản ghi cho mỗi lần Omni
phát biểu, tạo ra **lúc phát biểu**, không phải lúc biết kết quả.

Không có nó thì mỗi năng lực tự bịa một nguồn dữ liệu riêng → bốn phiên bản sự thật.

## Nguyên tắc bất di bất dịch

**A. Mẫu số chốt trước khi biết kết quả.** Ca được mở lúc advisory phát ra, với
`pattern_key` đóng băng. Sau đó không loại được ca nào, không đổi được nhóm.
*(Chặn bùa số: chọn mẫu, nắn pattern_key, chọn cửa sổ thời gian.)*

**B. Im lặng là im lặng.** Ba trạng thái: `CORRECT` / `INCORRECT` / `UNJUDGED`.
Ca `UNJUDGED` không vào tử số lẫn mẫu số của độ chính xác — nhưng tỉ lệ UNJUDGED
hiện ra và tự nó chặn việc xin quyền.
*(Chặn: im lặng = đúng.)*

**C. Sự thật đến từ thế giới.** Nhãn mạnh nhất là `recurred` — sự cố có tái diễn
không. Omni không bịa được vì đo từ hệ thống khách.

**D. Người chấm không phải người làm.** Verdict chỉ đến từ nguồn ngoài Omni:
Telegram (người bấm), HITL (người phán quyết), portal (admin), hoặc `world`
(tái diễn / đo lại). **Không có nguồn `self`.**

## Hai nhãn, cố ý tách rời

`diagnosis_verdict` — nguyên nhân nói có trúng không.
`remedy_verdict` — làm theo có hết không.

Hai cái lệch nhau thường xuyên (đoán trúng mà cách xử lý dở; đoán trật nhưng hành
động vẫn cứu được). Gộp một nhãn là mất thông tin vĩnh viễn — không bao giờ biết
nó yếu ở khâu nào. Thang 3 trong mục tiêu ("root cause **và** xử lý triệt để") vốn
là hai năng lực.

## Từ chối cũng là một ca

`posture` ghi Omni đã làm gì với ca đó:

| posture | nghĩa |
|---|---|
| `DIAGNOSED` | có chẩn đoán và khuyến nghị |
| `REFUSED` | không đủ tự tin, đẩy lên người kèm bằng chứng |
| `OUT_OF_SCOPE` | chẩn đoán được nhưng ngoài quyền hạn (code/kiến trúc) |

**Đây là cơ chế chống bùa số quan trọng nhất.** Nếu chỉ đo độ chính xác, chiến lược
tối ưu không phải nói dối mà là **từ chối mọi ca khó** — giữ hồ sơ 100% trên việc
dễ. Trông như cẩn thận, thực chất vô dụng.

Nên báo cáo luôn là **hai số kéo ngược nhau**:
- **Độ chính xác** = CORRECT / (CORRECT + INCORRECT), trong các ca `DIAGNOSED`
- **Độ phủ** = DIAGNOSED / (DIAGNOSED + REFUSED)

Ép cả hai cùng đẹp thì chỉ còn một cách: làm thật.

## Trí nhớ

`occurrence_no` + `prior_case_id` — cùng `pattern_key` trong cửa sổ nhớ.

Lần ≥2 **không được chẩn đoán lại từ đầu**. Phản hồi phải là: *"Đây là lần N. Tôi đã
báo ngày X, nguyên nhân Y, chưa ai xử lý."* Điều tra lại từ đầu là hành vi của người
mới — nếu làm vậy thì kinh nghiệm lần 1 vứt đi đâu?

Mức khẩn tăng theo `occurrence_no`. Lần 5 không thể trình bày cùng giọng lần 1.

## Bất biến cưỡng chế ở tầng DB (trigger, không phải quy ước)

1. `pattern_key`, `case_id`, `opened_at`, `posture` **không sửa được** sau khi ghi.
2. Verdict **không quay về** `UNJUDGED`.
3. Đổi verdict đã có → phải ghi `case_verdict_history` (append-only) kèm actor.
4. `verdict_source` không nhận giá trị `self`/`system`.

Đặt ở DB vì đây là bằng chứng khách hàng dùng để trao quyền — quy ước trong code
Python thì một lần refactor là mất.

## Cận dưới, không dùng điểm ước lượng

3/3 = 100% là vô nghĩa. Dùng **cận dưới Wilson 95%** → ít mẫu tự động không đủ điều
kiện, không cần ngưỡng `n` tuỳ tiện, không thể xin quyền bằng vài ca may mắn.

## Xin quyền

Omni **chủ động xin theo từng `pattern_key`**, không xin nâng tier tổng — bằng chứng
nó có là bằng chứng theo loại việc, không chứng minh được chỗ chưa gặp bao giờ.

- Bị từ chối → khoá xin lại loại đó một thời gian (nếu miễn phí thì chiến lược tối ưu
  là xin tới lúc admin mệt mà duyệt — lỗ hổng con người, có thật).
- `FROZEN` **chỉ người gỡ được**. Tự lên bậc được, không tự gỡ án được.
- Báo cáo phải **tái dựng được từ CRAT bởi khách**. Không con số nào không truy được
  về ledger. Nếu chỉ đưa bản tóm tắt do LLM viết thì bùa số nằm ở khâu kể chuyện,
  chưa cần đụng dữ liệu.

## Vòng đời tier = quá trình thử việc

`shadow` (≈3 tháng, quan sát) → `minimal` (được làm vài loại) → `autonomous`
(toàn quyền **trong khuôn khổ**). Chuyển tier là quyết định của **admin tenant**,
dựa trên bằng chứng Omni đưa. Khuôn khổ cấu hình trên portal, và portal **không phải
form trống** — Omni đề xuất sẵn, khách duyệt hoặc sửa.

Bất kể tier: hành động **xoá dữ liệu** luôn phải báo admin khách. Lằn ranh cứng.
