# ADR-001: Canonical Agent Runtime (AOIP vs remote_agent)

> **Xem thêm (2026-07-22)**: tiến độ migrate ADR-001 này được re-verify trong domain #5
> (omni-remote-agent) của ma trận năng lực đầy đủ 18 domain ở đầu
> [ASSESSMENT_autonomous_sre_v2.md](ASSESSMENT_autonomous_sre_v2.md) — kết luận không đổi:
> `aoip.agent.employee` chạy thật 3/3 VM, `aoip.agent.daemon` (canonical target) vẫn chỉ
> demo/proof script.

**Ngày:** 2026-07-03
**Trạng thái:** Accepted — **MIGRATION HOÀN TẤT trên fleet lab (2026-07-13, Sprint NV-SRE IT-4→IT-7)**

> **Cập nhật 2026-08-11 — CUTOVER DỨT ĐIỂM, unit cũ đã GỠ HẲN:**
> `omni-remote-agent.service` **đã bị xoá khỏi cả 3 VM lab** (không còn `disabled` giữ làm rollback
> path như bản cập nhật IT-7 dưới đây từng dự định). **Lý do đổi quyết định:** chính việc
> "giữ-disabled-làm-rollback" đã khiến bug 2-agent-song-song tái phát — ngày 2026-08-04 một lệnh
> `systemctl enable --now omni-remote-agent.service` chạy trên cả 3 VM (chọn nhầm unit vì
> `CLAUDE.md` khi đó mô tả sai rằng 2 unit chỉ là một cái đổi tên) đã hồi sinh unit cũ **im lặng**,
> gây double-fire toàn bộ evidence suốt 7 ngày. Sau khi gỡ hẳn, đúng lệnh đó sẽ báo lỗi
> `Unit file omni-remote-agent.service does not exist.` thay vì âm thầm tạo process trùng.
> Bằng chứng root-cause đầy đủ (mtime symlink + journal + transcript):
> `docs/audit/regression_agent_dual_process_2026-08-11.md`. Kế hoạch thực thi:
> `plans/consolidate-vm-agent-remote-to-aoip-employee-2026-08-11.md`.
>
> Runtime production duy nhất trên VM khách hàng: `aoip-agent.service` → `aoip.agent.employee`.
> **`aoip.agent.daemon` (§1 bên dưới, canonical target dài hạn) vẫn CHƯA từng deploy thật** — chỉ
> tồn tại trong demo/proof script. Không đổi trạng thái này trong đợt cutover 2026-08-11.
>
> **Nợ kỹ thuật §5 vẫn CHƯA fix (tính tới 2026-08-11):** `src/gateway/routes/agent_runtime.py`
> vẫn duplicate command-lifecycle logic của `aoip.agent.delivery.DurableCommandChannel` (state
> transition, fencing, visibility timeout, idempotency). Lý do kỹ thuật ban đầu (Gateway không
> import được `aoip`) đã hết hiệu lực từ commit `409dcb2`, nhưng việc hợp nhất cần một task/ADR
> riêng — **cố ý nằm ngoài phạm vi** đợt cutover này (ràng buộc "không thêm tính năng mới").

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
