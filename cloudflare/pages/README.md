# Landing page — `www.omnisre.xyz`

HTML tĩnh một file, self-contained. Không build step, không dependency, không asset
ngoài. Đây là điều kiện để dùng Cloudflare Pages Free mà không cần CI.

## Cấu hình Cloudflare Pages

| Trường | Giá trị |
|---|---|
| Framework preset | **None** |
| Build command | *(để trống)* |
| Build output directory | `cloudflare/pages` |
| Root directory | `/` |
| Production branch | `main` |

Custom domain: `www.omnisre.xyz`.

Không cần GitHub Actions — tích hợp Pages ↔ GitHub native đã đủ. Thêm workflow riêng
chỉ tạo thêm chỗ để rò rỉ API token.

## Ràng buộc phải giữ

- **Không thêm asset ngoài.** `_headers` đặt CSP `default-src 'none'`; một thẻ
  `<script src>` hay font CDN sẽ bị chặn im lặng.
- **Không nhúng secret.** Trang này công khai với toàn Internet, không có Access.
- **Không claim chưa chứng minh được.** Mục `Status` cố ý nói thẳng đây là hệ thống
  giai đoạn lab. Giữ nguyên tinh thần đó.

## Nguồn gốc

Migrate từ `/Users/hiendang/omni-site/index.html` (ngoài repo). Bản trong repo là
bản chính thức duy nhất — production không phụ thuộc file ngoài repo nữa. Đã thêm:
canonical/OG URL, favicon inline SVG, và link tới `app.omnisre.xyz`.
