# Backup systemd unit — `omni-remote-agent.service` (đã gỡ 2026-08-11)

Đây là bản sao **nguyên văn** file unit `/etc/systemd/system/omni-remote-agent.service` lấy từ
3 VM lab ngay TRƯỚC khi gỡ hẳn (Phase 4 của
`plans/consolidate-vm-agent-remote-to-aoip-employee-2026-08-11.md`).

**Vì sao phải lưu:** repo KHÔNG có template nào tái tạo được unit này —
`scripts/omni-agent-install.sh` không chứa định nghĩa của nó, và `scripts/aoip-agent.service`
là unit KHÁC. Nếu không có thư mục này, sau khi gỡ sẽ không còn đường rollback nào ngoài viết
lại unit thủ công từ trí nhớ.

**Đây KHÔNG phải file để deploy lại.** Runtime production duy nhất trên VM khách hàng hiện nay là
`aoip-agent.service` (`aoip.agent.employee`) — xem ADR-001. Chỉ dùng thư mục này nếu cần rollback
khẩn cấp về runtime cũ:

```bash
orb -m <vm> sudo tee /etc/systemd/system/omni-remote-agent.service \
  < docs/audit/backup-units/omni-remote-agent-<vm>-2026-08-11.service.bak
orb -m <vm> sudo systemctl daemon-reload
orb -m <vm> sudo systemctl enable --now omni-remote-agent.service
# LƯU Ý: bật lại cái này SONG SONG với aoip-agent.service sẽ tái tạo đúng bug double-fire
# evidence đã mất 7 ngày mới phát hiện. Nếu rollback, phải stop aoip-agent trước.
```

⚠️ Cả 3 file `.bak` giống hệt nhau về nội dung (cùng 16 dòng) — giữ riêng theo VM để chứng minh
đã lấy từ từng máy thật, không phải copy 1 bản suy ra 3.
