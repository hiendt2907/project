# Đồng bộ taxonomy domain + catalogue lệnh chẩn đoán khai báo

## Hiện trạng (đã khảo sát runtime, không suy diễn)

### Ba từ vựng "domain" không có cầu nối
| nguồn | giá trị | thực chất |
|---|---|---|
| `aoip/domain_adapters.py` | `linux` `kubernetes` `database` `network` | loại hệ thống khách |
| `pkg/reasoning/domain_signals.py` | `os_system` `network` `storage` `services` `container_logs` `database` `application` `security` | loại tín hiệu |
| `schemas/playbook.py` | `k8s` `os` `network` `service` `application` `api` `hardware` | loại việc playbook |

Ba tên cho K8s (`kubernetes`/`k8s`/`container_logs`), ba tên cho OS
(`linux`/`os`/`os_system`), `service` vs `services`. **Không có hàm map nào.**

### Lỗi thật: `domain` chứa hai loại giá trị khác bản chất
```
Redis  omni:playbook:grad:default:k8s:PB-K8S-CPU-RESTART   ← playbook_governor: domain KỸ THUẬT
PG     playbook_graduation.domain = 'advisory' (3 hàng)    ← advisory_promoter: NGUỒN HỌC
```
`advisory` không nằm trong 7 giá trị của `PlaybookDomain`. Và constraint bị mất đúng
chỗ cần nhất: `0002_playbook.sql` có `CHECK (domain IN (...))` trên bảng `playbook`
(dòng 9) nhưng bảng `playbook_graduation` (dòng 22) chỉ `TEXT NOT NULL`.

Hệ quả: `list_playbook_graduations()` — thứ `tier_loops`/`capacity_loops` đọc để đề
xuất **nâng tier** — trả hỗn hợp hai loại bản ghi. Đếm "bao nhiêu playbook tốt nghiệp"
là đếm gộp hai thứ khác nhau. Đây là con số dùng để **trao quyền tự chủ**.

### Lệnh chẩn đoán: hardcode ba chỗ, một chỗ bỏ qua hoàn toàn
1. `remote_agent/command_executor.py:83` — `COMMAND_WHITELIST` 24 lệnh, frozenset cứng.
2. `gateway/routes/agent_commands.py:83` — **bản sao thứ hai**, kèm comment *"Must stay
   identical"* vì `Dockerfile.gateway` không COPY `src/remote_agent/`. Đồng bộ bằng tay.
3. `remote_agent/collectors/*.py` — gọi `create_subprocess_exec` trực tiếp, **không qua
   validator nào**. Đang chạy `cat` — chính lệnh nằm trong `_CONTENT_READ_BLOCKED`.

Không có kubectl. Không có `ip route`/`dig`/`tcpdump`. Database chỉ `mysqladmin`.

## Thiết kế

### A. Một taxonomy duy nhất — `src/pkg/domain/`
Canonical: `kubernetes` `os_host` `network` `storage` `database` `service`
`application` `security` `hardware` `unknown`.

`ALIASES` map mọi giá trị của ba từ vựng cũ về canonical. Không xoá tên cũ ở biên
(dữ liệu lịch sử, payload agent cũ) — **chuẩn hoá khi đọc**, ghi luôn bằng canonical.

Tách hẳn khái niệm bị lẫn: `LearningTrack` (`advisory` | `playbook` | `execution`) là
**nguồn học**, không phải domain. Migration thêm cột `track` + backfill
`domain='advisory'` → `track='advisory'`, `domain='unknown'`, rồi thêm CHECK trên
`playbook_graduation.domain` — cái đang thiếu.

### B. Catalogue lệnh chẩn đoán khai báo — `config/diagnostic_commands.yaml`
Dữ liệu, không phải code. Mỗi entry:
```yaml
- command: kubectl
  domain: kubernetes
  subcommands: [get, describe, logs, top, events, api-resources, version, explain]
  deny_subcommands: [apply, delete, patch, scale, edit, exec, cp, drain, ...]
  deny_flags: [--token, --kubeconfig]
```
Loader `src/pkg/diagnostics/command_catalog.py` — **dùng CHUNG bởi gateway và agent**,
xoá bản sao thứ hai. Mở rộng không cần sửa code: `OMNI_DIAG_COMMAND_CATALOG` trỏ file
bổ sung, merge theo `command`.

Phủ đủ domain: `kubectl` · `ip/ss/dig/host/traceroute/mtr/arp/route/nft/iptables -L` ·
`mysql/psql/redis-cli/mongosh` (chỉ câu lệnh chẩn đoán) · `lvs/vgs/pvs/smartctl/iostat` ·
`journalctl/systemctl/dmesg` · `docker/podman/crictl` (chỉ ps/inspect/logs).

### C. Fail-closed ở tầng load, không ở tầng gọi
Catalogue **tự kiểm chính nó** lúc load: entry nào khai một subcommand nằm trong danh
sách động từ ghi (`apply|delete|patch|create|scale|drain|restart|stop|...`) thì **load
lỗi**, không phải cảnh báo. Người sửa file YAML không thể vô tình mở đường mutate.

Mutation vẫn **chỉ** đi qua đường cũ: K8s SDK (`MUTATE_TOOL_ALLOWLIST`) và capability
có kiểu (`aoip/capabilities/`, `MODE_HUMAN_APPROVED`). Catalogue này KHÔNG cấp quyền
mutate cho bất kỳ domain nào.

## ⚠️ Đánh đổi phải nói rõ trước khi làm

Yêu cầu "chạy **toàn bộ** lệnh chẩn đoán" **không tương thích** với
`INV_NO_DATA_EXFIL` ở dạng hiện tại. Invariant đó chặn cả `cat`, `grep`, `tail`,
`mysql`, `head` — vì đọc nội dung là rút dữ liệu khách. Nhưng chẩn đoán thật **cần**
đọc nội dung: `cat /proc/meminfo`, `tail /var/log/nginx/error.log`,
`mysql -e "SHOW SLAVE STATUS"`.

Nên thay **chặn theo tên lệnh** bằng **chặn theo phạm vi đọc**:
- Cho phép đọc: `/proc` `/sys` `/etc` `/var/log` `/run` và đường dẫn khai trong catalogue
- Chặn: home dir, thư mục dữ liệu DB (`/var/lib/mysql`, `/var/lib/postgresql`), backup,
  `.env`/key/secret theo mẫu tên
- DB: chỉ `SHOW` / `EXPLAIN` / `SELECT` trên schema hệ thống; `SELECT` trên bảng nghiệp
  vụ bị chặn

Đây là **nới lỏng có thật** so với hiện tại, đổi lấy năng lực chẩn đoán thật. Tôi làm
theo hướng này vì bạn yêu cầu rõ; nếu muốn giữ nguyên `INV_NO_DATA_EXFIL` chặt như cũ
thì Omni sẽ mãi không đọc được log ứng dụng — tức không thể chẩn đoán tầng app.

Invariant được viết lại thành `INV_DIAG_SCOPE_BOUNDED`: đọc được, nhưng chỉ trong phạm
vi vận hành đã khai báo, và mọi lần đọc đều vào CRAT.
