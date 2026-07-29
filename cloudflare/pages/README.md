# Landing page — `www.omnisre.xyz`

HTML tĩnh, song ngữ, **không JavaScript**, không build step, không dependency,
không tài nguyên từ host ngoài. Đó là điều kiện để CSP siết tới mức
`default-src 'none'` và để dùng Cloudflare Pages Free mà không cần CI.

```
cloudflare/pages/
├── index.html      EN  → https://www.omnisre.xyz/
├── vi/index.html   VI  → https://www.omnisre.xyz/vi/
├── style.css       dùng chung cho cả hai trang
├── _headers        security headers + CSP
├── _redirects      /console → app.omnisre.xyz
└── .nojekyll
```

## Vì sao hai trang riêng thay vì một nút chuyển ngôn ngữ

CSP chặn JavaScript nên toggle bằng JS là không thể. Nhét cả hai thứ tiếng vào một
DOM rồi ẩn bằng CSS cũng làm được, nhưng tệ hơn ở ba điểm: trình đọc màn hình vẫn
đọc phần đang ẩn, `<html lang>` chỉ khai báo được một giá trị, và không có link
riêng để chia sẻ.

Hai trang cho mỗi bản một `lang` đúng, `hreflang` đầy đủ (`en` / `vi` /
`x-default`), và URL chia sẻ được. CSS nằm ở một file duy nhất nên không có nguy cơ
hai bản trôi lệch nhau về giao diện.

**Sửa nội dung phải sửa CẢ HAI file.** Không có cơ chế nào ép điều đó — hãy tự kiểm.

## Cấu hình Cloudflare Pages

| Trường | Giá trị |
|---|---|
| Framework preset | **None** |
| Build command | *(để trống)* |
| Build output directory | `cloudflare/pages` |
| Root directory | `/` |
| Production branch | `main` |

Custom domain: `www.omnisre.xyz`.

Không cần GitHub Actions — tích hợp Pages ↔ GitHub native đã đủ. Quan trọng hơn:
**Pages build trên hạ tầng Cloudflare, không tiêu phút GitHub Actions** (repo này đã
gỡ toàn bộ workflow vì hết quota, xem commit `2038de1`).

## Ràng buộc bảo mật — đọc trước khi sửa

Trang này công khai với toàn Internet và **không có Cloudflare Access ở trước**,
khác hẳn `app.omnisre.xyz`. CSP hiện tại:

```
default-src 'none'; style-src 'self'; img-src 'self' data:;
form-action 'none'; frame-ancestors 'none'; base-uri 'none';
upgrade-insecure-requests
```

Không có `script-src` nào cả — `default-src 'none'` chặn sạch. Cũng **không** có
`'unsafe-inline'` trong `style-src`, siết được vì CSS đã tách ra file cùng origin.

Hệ quả phải biết:

- Thêm `<script>` bất kỳ ⇒ **bị chặn im lặng**, không có lỗi trên trang.
- Thêm font/ảnh/CDN ngoài ⇒ bị chặn im lặng.
- Thêm `style="..."` nội tuyến ⇒ bị chặn im lặng.

Nếu thật sự cần, hãy nới CSP **một cách có ý thức và tối thiểu**. Đừng thêm lại
`'unsafe-inline'` cho tiện.

Kiểm tra trước mỗi lần deploy:

```bash
grep -c '<script' index.html vi/index.html          # phải là 0 0
grep -n 'style="' index.html vi/index.html          # phải rỗng
grep -ohE '(src|href)="[^"#]*"' index.html vi/index.html | sort -u \
  | grep -vE 'mailto:|^href="/|omnisre.xyz|github.com|data:image'   # phải rỗng
```

## Nội dung

Mục `Status` / `Hiện trạng` cố ý nói thẳng đây là hệ thống giai đoạn lab. **Giữ
nguyên tinh thần đó** — chỉ ghi những gì đã kiểm chứng bằng cách chạy hệ thống.
Trang không được chứa hostname nội bộ, IP, tên namespace, hay bất kỳ chi tiết hạ
tầng nào ngoài những gì vốn đã công khai.

## Thiết kế

Hướng: *forensic instrument* — bảng đo của kỹ sư vận hành, không phải dark-mode SaaS
mặc định. Ba nguyên tắc: màu mang nghĩa cố định (xanh = đã kiểm chứng, hổ phách =
còn phía trước, đỏ = cơ chế dừng), số liệu là yếu tố typography chính, và mọi chuyển
động tự tắt khi `prefers-reduced-motion`. Trang cố ý chỉ có một diện mạo tối.
