# Regression: 2 agent process chạy song song trên 3 VM lab — root cause XÁC ĐỊNH

**Ngày điều tra:** 2026-08-11 (Phase 1 của `plans/consolidate-vm-agent-remote-to-aoip-employee-2026-08-11.md`)
**Kết luận:** Root cause xác định **bằng bằng chứng trực tiếp**, không phải suy luận.

## Tóm tắt

Ngày 2026-08-04 lúc `14:42:50.988Z` (= `21:42:51 +0700`), một phiên Claude Code chạy lệnh:

```bash
cd /Users/hiendang && for m in cust-edge cust-app cust-db; do \
  printf "=== %s ===\n" "$m"; \
  orb -m "$m" -u root systemctl enable --now omni-remote-agent.service 2>&1; \
done
```

Lệnh này `enable` + `start` **unit đã lỗi thời** (`omni-remote-agent.service`) trên cả 3 VM,
trong khi unit đúng đang chạy là `aoip-agent.service`. Từ thời điểm đó, cả 3 VM chạy 2 process
agent song song → double-fire toàn bộ evidence lane, kéo dài 7 ngày tới khi phát hiện 2026-08-11.

## Bằng chứng (4 nguồn độc lập, khớp nhau tuyệt đối)

### 1. Timestamp symlink `enable` trên VM thật

`systemctl enable` tạo symlink trong `multi-user.target.wants/`. mtime của symlink = thời điểm
lệnh chạy:

| VM | symlink `omni-remote-agent.service` mtime |
|---|---|
| cust-edge | `2026-08-04 21:42:51.549362011 +0700` |
| cust-app  | `2026-08-04 21:42:51.650362947 +0700` |
| cust-db   | `2026-08-04 21:42:51.770364060 +0700` |

So sánh: symlink `aoip-agent.service` có mtime `2026-07-08` (cust-app) / `2026-07-13` (cust-db,
cust-edge) — tức unit đúng đã được enable từ trước, không bị đụng vào ngày 08-04.

### 2. Thứ tự và khoảng cách thời gian khớp vòng lặp

Thứ tự mtime: **edge (.549) → app (.650) → db (.770)**, cách nhau ~101ms và ~120ms.
Khớp chính xác thứ tự trong vòng lặp `for m in cust-edge cust-app cust-db`.

### 3. Journal systemd trên VM

```
Aug 04 21:42:51 cust-app systemd[1]: Reloading requested from client PID 728 ('systemctl')...
Aug 04 21:42:51 cust-app systemd[1]: Reloading...
Aug 04 21:42:51 cust-app systemd[1]: Reloading finished in 84 ms.
Aug 04 21:42:51 cust-app systemd[1]: Started omni-remote-agent.service - Omni Remote Agent.
```
(`enable --now` = `enable` + `start`, systemd tự `daemon-reload` khi tạo symlink mới.)

### 4. Transcript phiên Claude Code

File: `~/.claude/projects/-Users-hiendang-project/48a26b0d-e15d-469d-baf6-013954b7f800.jsonl`
- Timestamp bản ghi tool_use: `2026-08-04T14:42:50.988Z` (UTC) = `21:42:50.988 +0700`
- `description` của lệnh: *"Enable and start remote agent on 3 VMs"*
- Ngữ cảnh: user nói *"Bật và xoá đi"* lúc `14:42:42.688Z` — trong phiên **tắt Kubernetes
  OrbStack + xoá dữ liệu k3s cũ** để giải phóng tài nguyên sau khi core đã di dời sang GCP.
  Ý user là bật lại agent trên VM (đúng), nhưng agent được bật là **unit sai**.

Chênh lệch giữa timestamp transcript (`.988`) và mtime symlink đầu tiên (`.549` của giây kế tiếp)
là ~560ms — đúng độ trễ khởi động tiến trình `orb` + `systemctl`.

## Vì sao chọn nhầm unit — nguyên nhân gốc của nguyên nhân gốc

`CLAUDE.md` dòng 411 (tại thời điểm đó, và vẫn còn tới hôm nay) ghi:

> `omni-remote-agent.service` (tên khác `aoip-agent` — đừng tìm nhầm unit).

Câu này **SAI**: nó khẳng định 2 tên chỉ là 1 unit đổi tên. Thực tế là 2 systemd unit khác nhau,
2 entrypoint khác nhau (`remote_agent.agent` vs `aoip.agent.employee`), cùng đọc chung
`/opt/omni-remote-agent/run.env`. Bất kỳ agent/người nào đọc CLAUDE.md rồi cần "bật agent trên
VM" đều sẽ chọn `omni-remote-agent.service` và tin rằng mình đang bật đúng thứ.

**Chuỗi nhân quả đầy đủ:**
```
CLAUDE.md dòng 411 mô tả sai (2 unit = 1)
  → phiên 2026-08-04 cần bật lại agent, chọn tên unit theo tài liệu
    → enable --now omni-remote-agent.service trên 3 VM
      → 2 process song song, double-fire evidence
        → tồn tại âm thầm 7 ngày (không có alert nào cho tình trạng này)
```

Đây là lý do Phase 5a (sửa CLAUDE.md) **không phải việc dọn dẹp tài liệu cho đẹp** — nó là một
phần của việc sửa root cause.

## Đối chiếu 3 nghi phạm đặt ra ban đầu trong plan

| # | Nghi phạm | Kết luận |
|---|---|---|
| 1 | `scripts/e2e_onboarding_full_flow.py:345-347` (`enable`+`start` vô điều kiện) | **KHÔNG phải thủ phạm lần này** — script chỉ nhắm `TARGET_VM = "cust-db"` (dòng 41), không giải thích được cả 3 VM. Nhưng **vẫn là bom hẹn giờ thật**: nếu chạy, nó sẽ tái tạo đúng bug này trên cust-db. Phase 5b vẫn phải sửa. |
| 2 | `scripts/enroll_remote_agent.py` (`UNIT = "omni-remote-agent"`, restart mỗi lần enroll) | **Không phải nguyên nhân khởi phát** (chỉ `restart`, không `enable`). Nhưng sau Phase 4 sẽ **gãy** (restart unit không tồn tại) → Phase 5b vẫn phải sửa. |
| 3 | Rollback drill thủ công (comment trong `aoip-agent.service`) | **Không phải nguyên nhân lần này.** Drill có thật (ghi trong `docs/product/PRODUCT_PROOF.md`: *"Rollback drill 2 chiều: disable aoip-agent → enable omni-remote-agent → active, emit 200... roll-forward → employee"*), nhưng diễn ra 2026-07-13 (IT-5) và đã được dọn bởi audit 2026-07-22 (journal cust-app: `Jul 22 14:26:20 Stopped omni-remote-agent.service`). |

Giả thuyết ban đầu (#3) mà tôi đưa ra trước khi điều tra là **SAI**. Thủ phạm thật là một lệnh
thủ công chạy 1 lần, không nằm trong script nào của repo — nghĩa là **grep repo sẽ không bao giờ
tìm ra nó**. Đây là lý do phải truy bằng mtime symlink + journal + transcript.

## Vì sao Phase 4 (gỡ hẳn) triệt tiêu được lớp bug này về mặt cấu trúc

Fix 2026-07-22 dùng `systemctl disable --now` — unit vẫn nằm trên VM, chỉ tắt. Bất kỳ ai/agent
nào sau đó gõ `systemctl enable --now omni-remote-agent.service` đều **thành công im lặng** và
tái tạo bug.

Sau Phase 4 (xoá file `/etc/systemd/system/omni-remote-agent.service`), chính lệnh đó sẽ trả về
`Failed to enable unit: Unit file omni-remote-agent.service does not exist.` — **lỗi thẳng, nhìn
thấy ngay**, thay vì hồi sinh âm thầm một process trùng lặp. Đây là khác biệt cốt lõi giữa fix
lần này và fix lần trước.

## Kết quả thực thi cutover (2026-08-11) — đã verify sống

| Kiểm chứng | cust-app | cust-db | cust-edge |
|---|---|---|---|
| Evidence rate TRƯỚC (lần/60s, interval=20s ⇒ đơn=3) | 6 | 7 | 6 |
| Evidence rate SAU khi stop unit cũ | **3** | **3** | 4 → **3** (đo lại) |
| Process agent còn lại | 1 | 1 | 1 |
| `systemctl status omni-remote-agent` | `could not be found` | `could not be found` | `could not be found` |
| `/opt/omni-remote-agent/remote_agent/` còn nguyên | ✅ | ✅ | ✅ |
| `aoip-agent.service` | active | active | active |
| Soak (NRestarts=0, registry tươi) | 30 phút PASS | 25 phút PASS | 25 phút PASS |

**Bằng chứng cơ chế chống-regression hoạt động thật** — chạy lại đúng lệnh đã gây bug:
```
$ orb -m cust-app -u root systemctl enable --now omni-remote-agent.service
Failed to enable unit: Unit file omni-remote-agent.service does not exist.
$ echo $?
1
```
Trước cutover, đúng lệnh này thành công im lặng và tạo process trùng. Nay nó **lỗi thẳng, exit 1**.

Drill thật (dừng `payment-api.service` trên cust-app, aoip-agent chạy một mình): Omni phát hiện
đúng `domain=service` `urgency=critical`, **0** cảnh báo `agent OFFLINE` → chứng minh
`aoip.agent.employee` một mình đủ đảm nhiệm vai trò tiếp nhận/vận hành/xử lý. (Vòng chẩn đoán sau
đó timeout do bottleneck LLM `qwen3:8b -np 1` — vấn đề độc lập, đã biết từ trước, không liên quan
cutover này.)

Backup unit file của cả 3 VM: `docs/audit/backup-units/` (đường rollback duy nhất — repo không có
template nào tái tạo được unit này).

## Bài học cần ghi nhớ (ngoài phạm vi sửa của plan này)

Không có cơ chế nào phát hiện tình trạng "2 agent cùng gửi 1 `agent_id`" trong suốt 7 ngày.
Cả 2 process cùng ghi vào cùng key registry Redis nên `KEYS omni:remote_agent:registry:*` luôn
trả về đúng 1 key/host — phép đo này **không thể** phát hiện double-fire (và chính nó đã được
dùng làm bằng chứng "đã fix" trong audit 2026-07-22, tạo ra false-negative). Đề xuất cho task
riêng sau: thêm phát hiện double-fire ở phía Omni (vd so tần suất evidence thực nhận với
`collect_interval` khai báo của agent). **Không nằm trong phạm vi plan hiện tại.**
