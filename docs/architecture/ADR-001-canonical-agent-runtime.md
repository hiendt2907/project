# ADR-001: Canonical Agent Runtime (AOIP vs remote_agent)

**Ngày:** 2026-07-03
**Trạng thái:** Accepted — **MIGRATION HOÀN TẤT trên fleet lab (2026-07-13, Sprint NV-SRE IT-4→IT-7)**

> **Cập nhật 2026-07-13 (IT-7 sprint close):** cả 3 VM lab (cust-edge/cust-app/cust-db) chạy
> `aoip.agent.employee` (unit `aoip-agent.service`, 1 process 2 vòng: telemetry reuse
> `remote_agent.run_agent()` as-library + AOIP durable command daemon) — agent 1.3.2, drift
> `current`, update/rollback qua chính durable command channel (IT-5), outcome bền PG (IT-6),
> soak reboot + cắt mạng 10 phút PASS với evidence outbox (IT-7). Unit cũ
> `omni-remote-agent.service` giữ disabled trên VM làm rollback path. §3-§4 bên dưới là trạng
> thái lịch sử tại thời điểm viết; §1-§2 vẫn hiệu lực (`aoip.agent.main` đã được employee thay
> thế trong provisioning thực tế).

> **Cập nhật 2026-07-03:** §5 (hướng "gateway import `DurableCommandChannel`") đã bị superseded
> bởi `ADR-002-command-protocol.md` sau khi đọc kỹ cả hai implementation — `agent_runtime.py` đã
> vượt bản aoip về an toàn (fencing/atomic claim/heartbeat), hướng hợp nhất đúng là hút state
> machine vocabulary ra `aoip.protocol` dùng chung. Các quyết định §1-§4 giữ nguyên hiệu lực.

> **Current reading rule (2026-07-14):** This ADR records the runtime migration history. For
> customer-system topology and API-sequence semantics, use
> [`customer-system-understanding.md`](customer-system-understanding.md); that view never
> draws Omni or Remote Agent as customer components.

## Bối cảnh

Repo hiện có ba entrypoint agent chạy trên host khách hàng, không có tài liệu nào chốt cái nào là
canonical:

| Entrypoint | Vị trí | Thực tế đang dùng ở đâu |
|---|---|---|
| `remote_agent.agent` | `src/remote_agent/agent.py` | `scripts/omni-agent-install.sh:243,263` — runtime đang chạy thật trên 3 VM lab (`cust-edge`, `cust-app`, `cust-db`), unit `omni-remote-agent.service` |
| `aoip.agent.main` | `src/aoip/agent/main.py` | `scripts/install_agent.sh:50` — installer khác, dùng systemd unit `aoip-agent`, chưa xác nhận đang chạy trên host thật nào |
| `aoip.agent.daemon` | `src/aoip/agent/daemon.py` | Chỉ reference trong `scripts/prove_durable_delivery.py:113` (demo/proof script) — chưa từng deploy thật |

`aoip.agent.daemon` là bản có nền tảng runtime an toàn nhất: durable local inbox/outbox, lease
renewal, fencing token, idempotency ledger, crash recovery, resume sau reboot (xem
`src/aoip/agent/{lease,idempotency,inbox,renewal}.py`). Đây chính là phần khó nhất của một agent có
quyền thay đổi hạ tầng khách hàng, nhưng nó chưa từng được deploy ngoài kịch bản chứng minh.

Song song đó, `src/gateway/routes/agent_runtime.py` tự nhận trong docstring là "bản twin phía
Gateway của `aoip.agent.delivery.DurableCommandChannel`", với lý do ghi trong docstring là
`Dockerfile.gateway` không `COPY src/aoip` nên Gateway không thể import trực tiếp. **Lý do này đã
lỗi thời**: commit `409dcb2` (2026-07-02, "fix(gateway): bundle src/aoip/ so Competency/Unknowns API
actually works") đã thêm `COPY src/aoip/ /app/src/aoip/` vào `Dockerfile.gateway:30` để phục vụ
route `/onboarding/competency`, `/unknowns`, `/questions*`. `aoip.agent.delivery` chỉ phụ thuộc
stdlib (`json`, `time`, `dataclasses`) nên Gateway hoàn toàn import trực tiếp được ngay bây giờ —
docstring trong `agent_runtime.py` chưa được cập nhật theo. Hai bản triển khai độc lập của cùng
command lifecycle (state transition, fencing, visibility timeout, idempotency) vẫn tồn tại và có
rủi ro drift thật, nhưng lý do kỹ thuật ban đầu (không import được) không còn đúng — việc hợp nhất
`agent_runtime.py` để import trực tiếp `aoip.agent.delivery` thay vì duplicate là khả thi về mặt kỹ
thuật ngay bây giờ, không cần tách package `aoip_protocol` riêng như đề xuất ban đầu.

## Quyết định

1. **`src/aoip/agent/daemon.py` là target runtime dài hạn (canonical).** Mọi tính năng agent mới
   (durable command execution, capability mới, mutation executor) phát triển trên nền AOIP, không
   trên `remote_agent`.
2. **`aoip.agent.main` là bước trung gian** — đang được `scripts/install_agent.sh` sử dụng, giữ
   nguyên cho tới khi `aoip.agent.daemon` đủ trưởng thành để thay thế nó trong installer.
3. **`src/remote_agent/agent.py` là runtime đang chạy thật trên VM lab, giữ nguyên trong scope
   này.** Không migrate 3 VM lab sang AOIP ngay. Việc migrate remote_agent → AOIP là một
   phase/task riêng, cần kế hoạch runtime-verify trên VM thật trước khi thực hiện (không thuộc
   scope ADR này).
4. **Không tạo Deployment/manifest mới cho AOIP daemon trong scope này** — quyết định này chỉ chốt
   hướng đi, việc triển khai thật để phase sau.
5. **Rủi ro drift giữa `agent_runtime.py` và `aoip.agent.delivery`** được ghi nhận nhưng chưa xử lý
   trong ADR này. Vì `Dockerfile.gateway` đã COPY `src/aoip/` từ `409dcb2`, hướng sửa rẻ hơn đề
   xuất ban đầu (tách package `aoip_protocol` riêng) là: đổi `agent_runtime.py` sang import trực
   tiếp `aoip.agent.delivery.DurableCommandChannel` thay vì duplicate logic — việc này để một task
   riêng, không thuộc scope quick-win này (cần đọc kỹ toàn bộ `agent_runtime.py` để xác nhận không
   phá vỡ HTTP contract hiện có với agent thật đang chạy).

## Hệ quả

- Không có thay đổi runtime ngay lập tức. VM lab tiếp tục chạy `remote_agent.agent`.
- Feature work agent mới ưu tiên viết trên AOIP domain (`src/aoip/`), không mở rộng thêm
  `src/remote_agent/`.
- `docs/vendor/OMNI_PROJECT_CANONICAL.md` được cập nhật với một dòng trỏ tới ADR này để người đọc
  tài liệu canonical biết hướng runtime đã được chốt.
- Việc migrate VM lab thật, xây `aoip_protocol` package dùng chung, và triển khai
  `aoip.agent.daemon` trên hạ tầng thật là các quyết định/công việc riêng, cần một ADR hoặc kế
  hoạch runtime-verify tiếp theo trước khi thực hiện.
