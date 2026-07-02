# Safety Policy

## Mặc định bắt buộc

`OMNI_AUTO_EXECUTE_ENABLED=false` — kill-switch toàn hệ thống. Skill KHÔNG được tự bật biến này
trong bất kỳ hoàn cảnh nào (kể cả "lab", kể cả để test closed-loop). Bật auto-execute là quyết định
của người dùng, không phải của agent tự trị.

## Được tự làm trong lab/dev (không cần hỏi lại)

- Đọc/sửa repository
- Thêm test
- Build image
- Deploy workload lab
- Rollout do image/config thay đổi
- Chạy migration canonical, reversible, trong lab
- Tạo tenant test
- Provision Agent lab
- Restart Agent lab do config của iteration hiện tại
- Gọi API test
- Đọc Redis/Kafka/DB
- Chạy smoke test không destructive
- Cập nhật docs
- Commit local sau khi acceptance pass

## Phải dừng chờ người dùng (STOP, không tự quyết)

- Production/customer thật
- Xóa dữ liệu
- Drop/truncate schema
- Migration không rollback được
- Thay firewall/DNS/certificate ngoài lab
- Rotate/revoke credential
- Bật auto-execute
- Arbitrary remote shell
- Generic Remote Agent command (không typed/allowlisted)
- Blast radius không rõ
- Secret leak (nghi ngờ hoặc phát hiện)
- Source of truth không xác định (nhiều nơi claim canonical, mâu thuẫn nhau)
- Push/merge/release
- Quyết định sản phẩm có nhiều lựa chọn ngang nhau (không có lựa chọn rõ ràng tốt hơn)

## Quy tắc chung

- Không in secret ra output/log/commit message.
- Không tự dùng quyền bypass vô hạn (`--dangerously-skip-permissions` và tương đương bị cấm theo
  mặc định). **Ngoại lệ duy nhất**: `scripts/supervisor.sh` dùng flag này cho các invocation
  `claude -p` không tương tác của nó, vì `-p` không có TTY nên mọi Edit/Write/Bash bị auto-deny
  nếu không có override — đây là quyết định user đã xác nhận tường minh 2 lần sau khi được cảnh báo
  rủi ro (2026-07-02), KHÔNG phải hành vi mặc định của skill này. `OMNI_AUTO_EXECUTE_ENABLED=false`
  là kill-switch riêng, không liên quan và không bị ảnh hưởng bởi ngoại lệ này.
- Không sửa live Kubernetes object trực tiếp (`kubectl edit`/`kubectl patch` ad hoc) mà bỏ quên
  source of truth trong git — mọi thay đổi manifest phải quay lại repo.

## Remote Agent invariants (chi tiết → operating-model.md)

Remote Agent = sensor + typed executor. Discovery tenant/agent/host-scoped, periodic, có
provenance/timestamp/content-hash, tuân thủ data residency. Mutation typed/allowlisted/policy-
controlled/reversible/có lease+idempotency+post-verification+reconciliation+audit. Không bao giờ
gọi external mutation là exactly-once.

## LLM invariants (chi tiết → operating-model.md)

LLM diễn giải/phân loại/đề xuất — không tự tuyên bố Fact, không tự nâng Claim VERIFIED, không tự bỏ
qua policy, không tự chọn action nguy hiểm, không tự xác nhận UnderstandingComplete. Deterministic
policy luôn là authority.
