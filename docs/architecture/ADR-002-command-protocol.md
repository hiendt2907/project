# ADR-002: Canonical Command Delivery Protocol

**Ngày:** 2026-07-03
**Trạng thái:** Accepted

## Bối cảnh

Command delivery lifecycle (mutating recovery command, Omni → agent) hiện có hai implementation:

| Implementation | Vị trí | Năng lực | Ai dùng thật |
|---|---|---|---|
| Gateway HTTP routes | `src/gateway/routes/agent_runtime.py` (469 dòng) | atomic Lua claim, fencing token, `delivery_attempt`, `record_version`, visibility heartbeat, ownership guard 409 | **Runtime thật**: `aoip.agent.omni_client` ↔ `DeliveryLoop` ↔ daemon; E2E `test_m1_systemd_recovery_e2e.py` |
| `DurableCommandChannel` | `src/aoip/agent/delivery.py` (233 dòng) | PEEK-not-POP, TTL, terminal ack — **không có** fencing/atomic claim/heartbeat; poll có race non-atomic | Chỉ tests (`test_aoip_delivery.py`) + demo (`scripts/prove_durable_delivery.py`) |

ADR-001 §5 từng đề xuất hướng rẻ "đổi `agent_runtime.py` sang import
`aoip.agent.delivery.DurableCommandChannel`". **Sau khi đọc kỹ cả hai file, hướng đó là sai
chiều**: `agent_runtime.py` đã tiến hoá vượt `DurableCommandChannel` về an toàn (fencing chống
lost-update giữa hai Gateway worker, heartbeat cho mutation chạy lâu). Import bản yếu hơn vào
Gateway sẽ là downgrade an toàn thật.

Phần thực sự bị duplicate và có rủi ro drift là **state machine vocabulary**: bộ hằng số
`QUEUED/DELIVERED/ACCEPTED/RUNNING/RECONCILING/COMPLETED/FAILED/ESCALATED/EXPIRED`, tập TERMINAL,
tập PROGRESS — được định nghĩa độc lập ở `agent_runtime.py:74-78`, `delivery.py:26-42`, và lần
thứ ba **bên trong Lua script** (`_CLAIM_SCRIPT` hardcode bảng `TERMINAL`). Thêm một state mới
(ví dụ `CANCELLED`) mà quên một trong ba chỗ sẽ tạo bug im lặng.

## Quyết định

1. **Canonical protocol = HTTP contract + state machine của `agent_runtime.py`** (fencing,
   attempt, version, heartbeat). Đây là nguồn chân lý cho command delivery lifecycle.
2. **Tạo module canonical `src/aoip/protocol/`** chứa duy nhất vocabulary + invariants của state
   machine: states, TERMINAL, PROGRESS, legal transitions, protocol version. KHÔNG chứa
   transport/persistence — chỉ pure constants + pure functions (stdlib-only) để mọi phía
   (Gateway, agent daemon, tests, script) import chung. Gateway được phép import `aoip`
   (tiền lệ: `gateway/routes/onboarding.py`; `Dockerfile.gateway` đã COPY `src/aoip/`).
3. **`agent_runtime.py` và `delivery.py` import states từ `aoip.protocol`** thay vì tự định
   nghĩa. Lua script không import được → thêm **contract test** khẳng định bảng TERMINAL trong
   Lua source khớp `aoip.protocol.TERMINAL_STATES` (drift = test fail).
4. **`DurableCommandChannel` là legacy/reference**, không phải canonical. Sunset criteria: khi
   Phase 3 Slice 3 (PostgreSQL command source of truth) triển khai, mọi test đang dùng nó phải
   chuyển sang test HTTP contract hoặc bị xoá cùng module. Không thêm feature mới vào nó.
5. **Không tạo thêm** command lifecycle, transport abstraction, hay persistence model cạnh
   tranh nào khác (nhắc lại từ master plan).

## Feature flag / rollback

Bước 2-3 là refactor import-only, hành vi runtime không đổi (bytes state string y hệt) — rollback
= revert commit. Không cần feature flag. Mọi thay đổi HÀNH VI protocol sau này (thêm state, đổi
transition) phải bump `aoip.protocol.PROTOCOL_VERSION` và có compatibility note.

## Hệ quả

- Một chỗ duy nhất để đọc/mở rộng state machine; drift giữa 3 bản chép tay bị chặn bằng test.
- Phase 3 Slice 3 (durable Control Plane trên PostgreSQL) sẽ xây trên vocabulary này, không
  phát minh lại.
- ADR-001 §5 được coi là superseded bởi ADR này ở điểm "import DurableCommandChannel".

## Alternatives đã loại

- **Package `aoip_protocol/` top-level riêng** (đề xuất gốc master plan): thừa — `src/aoip/` đã
  được cả hai image COPY; thêm top-level package mới tạo thêm một đường đồng bộ Dockerfile nữa.
- **Gateway import `DurableCommandChannel`**: downgrade an toàn (mất fencing/heartbeat) — loại.
- **Port fencing vào `DurableCommandChannel` rồi gateway dùng nó**: viết lại lớn không cần thiết;
  Gateway routes đang chạy thật và đã được E2E chứng minh — giữ nguyên, chỉ hút vocabulary ra.
