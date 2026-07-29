# Provider portal — các domain bị loại trừ có chủ đích

> Gom từ 7 file `app/<route>/page.tsx` chỉ render `SectionStub`. Các route đó đã **xoá**
> ngày 2026-07-29 (không nằm trong `PROVIDER_NAV` nên người dùng không thấy, nhưng vẫn
> truy cập được bằng URL trực tiếp — 7 trang rỗng nằm trong routing tree là nợ điều hướng).
> **Lý do loại trừ thì vẫn còn giá trị**, nên chép nguyên vào đây thay vì mất theo file.

Nguyên tắc bao trùm — `lib/nav.ts` GOVERNING RULE (2026-07-01):

> Navigation CHỈ phản ánh capability backend/runtime đã tồn tại. Portal là "operational
> projection" của runtime — KHÔNG phải product portal. Không billing/CRM/deployment/
> policy-editor/onboarding-wizard trừ khi backend đã production-ready.

## Chặn vì lý do AN TOÀN (không chỉ vì chưa làm)

### Policies — autonomy policy editor
`omni-ui /config/autonomy` đổi autonomy tier qua endpoint gateway `/autonomy/policy`
**KHÔNG tenant-scoped** (áp dụng toàn cluster) và chưa có authorization theo tenant tương
đương các write-action khác. Port trực tiếp sẽ cho phép một operator đổi autonomy policy
ảnh hưởng auto-execute pipeline của **TẤT CẢ** tenant.
**Điều kiện mở khoá**: backend contract mới — tenant-scoped policy write + authorization.

### Users — quản lý user/RBAC
Cần backend contract tenant-scoped trước khi triển khai.

### Customers — tenant registry / CRM
Write-action (issue/revoke API key, suspend tenant) cần thiết kế bảo mật riêng.
Ghi chú 2026-07-29: `/tenants` (đã live) phủ phần đọc; `GET`/`POST` api-keys nay đã được
gate `_require_admin_ctx` sau khi vá lỗ hổng phân quyền `/autonomy`.

## Chặn vì KHÔNG PHÙ HỢP mô hình đa tenant

### Deployments — deployment center
`omni-ui /deploy` hiển thị trạng thái **cluster-wide**, không phải projection tenant-scoped.
Nút "Rollout"/"Restart" chỉ trả mock acknowledgement (route POST không trigger K8s thật) —
không có backend contract thật để port.

## Chặn vì TRÙNG với route đã live

### Systems
System Twin đã hiển thị ở **Understanding** (entity graph). Route riêng chưa có nav slot.

### Onboarding
Readiness đã cover ở **Understanding**.

## Ứng viên tốt nhất cho slice kế tiếp

### Platform Health
Khác hai nhóm trên: `omni-ui /workers` (Worker Fleet health) **thực ra read-only và
tenant-scope được** — cùng nguồn gateway `/agents` đã dùng ở route `/agents`
(`lib/agents.ts`). Chưa port chỉ vì thêm mục nav mới là quyết định kiến trúc (mở rộng
`PROVIDER_NAV`), không tự ý làm trong 1 iteration.

---

Tham chiếu gốc: `docs/plans/aoip-provider-portal-slices.md`.
