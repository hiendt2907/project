# Audit Đ51 — 9 domain: evidence thật → xử lý → Telegram → giá trị vận hành

**Ngày:** 2026-08-11 · **Phạm vi:** GCP UAT cluster `multi-agent` + 3 VM lab (loyalty-uat)
**Phương pháp:** đo trên dữ liệu runtime thật (989 diagnosis session trong Redis, 1207 trace key,
log pod, gọi LLM trực tiếp, chạy collector trên VM). **Không có số liệu nào là ước lượng.**

Mọi lệnh dùng để lấy số đều ghi trong tài liệu này để tái kiểm.

---

## 0. Kết luận một dòng

Đường ống chạy **đúng về mặt cơ khí** (evidence → RAG → LLM → CRAT → Telegram, không mất tin),
nhưng **2.2% tin nhắn Telegram có giá trị cho người vận hành**. 97.8% còn lại là thẻ báo động
không kết luận được gì, kèm 4 câu khuyên chung chung (`df -h`, `free -h`). Nguyên nhân gốc **không
phải** logic domain — mà là **LLM chỉ có 1 slot xử lý** và nhu cầu vượt công suất, khiến 74% lượt
suy luận chết vì timeout.

---

## 1. Số liệu tổng — 989 diagnosis session thật

Nguồn: `omni:diag:session:*` trong Redis (`redis-cli --scan`), toàn bộ 989 bản ghi.

| Chỉ số | Giá trị | Ý nghĩa |
|---|---|---|
| Tổng session | **989** | 16 giờ quan sát, ~61.8 ca/giờ |
| Confidence = 0.0 | **767 (77.6%)** | Không ra được kết luận nào |
| Remediation là "generic fallback" | **909 (91.9%)** | Lời khuyên không gắn với host |
| Được gửi lên Telegram | **989 (100%)** | Không có cái nào bị chặn |
| **Thực sự hữu ích** (conf ≥0.7 **và** không generic) | **22 (2.2%)** | |
| Lượt LLM lỗi timeout | **2059 / 2773 (74.3%)** | |
| Lệnh chẩn đoán đã chạy thật trên VM | 1423 | Phần cơ khí hoạt động tốt |

**Kiểm chứng lại trên 6 giờ gần nhất** (sau cutover Đ50, để loại nghi ngờ "số liệu cũ"):
436 session → **3.0% hữu ích, 81.0% confidence 0.0**. Tình trạng **không** cải thiện theo thời gian.

### Phân bố theo domain (thực tế, không phải thiết kế)

| Domain | Session | conf=0 | generic | Hữu ích | avg conf | Probe thật |
|---|---:|---:|---:|---:|---:|---|
| `service` | 507 | 403 | 468 | 7 | 0.09 | `service_systemd_units` |
| `application` | 444 | 340 | 409 | 13 | 0.14 | `remote_log_errors` |
| `security` | 20 | 12 | 17 | 1 | 0.17 | `security_privilege_escalation` |
| `network` | 8 | 6 | 8 | 0 | 0.12 | `network_listeners`, `remote_port_check` |
| `database` | 6 | 4 | 6 | 0 | 0.13 | `mysql_health` |
| `os_host` | 2 | 1 | 0 | 1 | 0.35 | `remote_system_metrics` |
| `storage` | 2 | 1 | 1 | 0 | 0.47 | `remote_disk_usage` |
| `kubernetes` | **0** | — | — | — | — | *(xem §4)* |
| `hardware` | **0** | — | — | — | — | không có collector |

**`service` + `application` = 96.2% toàn bộ tải.** 5 domain còn lại cộng lại chưa tới 4% — chúng
tồn tại trong code và có chạy, nhưng gần như không có mẫu thật để đánh giá chất lượng.

---

## 2. Một evidence hoàn chỉnh → Telegram thật sự trông thế nào

Dựng lại bằng **chính hàm render của hệ thống** (`workers.remote_diagnosis_emitter
.render_diagnosis_session`), không phải mô phỏng.

### 2a. Trường hợp điển hình (92% — session `ra-670397d23209`, domain `service`)

```
🔵 [REMOTE DIAG] loyalty-uat_cust-edge
[cust-edge] systemd: 3 units failed/activating · CUSTOM_APP: [...]

📍 VẤN ĐỀ
Diagnosis inconclusive — no infrastructure-error-free turn produced a hypothesis
Confidence: 0%

🎯 ẢNH HƯỞNG
• Chưa xác định được component bị ảnh hưởng

🔍 ĐÃ CHẨN ĐOÁN (2 turns)
Turn 1: llm_error
Turn 2: llm_error

🛠️ CẦN LÀM
1. [generic fallback — not host-specific; verify before running]
2. Review system logs: journalctl -xe -n 200
3. Check disk: df -h
4. Check memory: free -h
5. Check failed services: systemctl --failed
```

**Giá trị cho người vận hành: gần bằng không.** Thẻ này nói "có 3 unit failed" (thông tin người
vận hành tự `systemctl --failed` là ra trong 1 giây) rồi thú nhận không chẩn đoán được, và khuyên
4 câu áp dụng cho mọi sự cố trên đời. Nó **tệ hơn không gửi**, vì nó tiêu tốn sự chú ý và dạy
người trực bỏ qua cảnh báo.

### 2b. Trường hợp tốt nhất (2.2% — `ra-4b655063592b`, domain `application`, conf 0.95)

```
🔴 [REMOTE DIAG] staging-sim_cust-app
📍 VẤN ĐỀ
payment-api service is active but not handling required endpoints (/api/health,
/webhook/*) leading to ConnectionRefusedError and application crashes  — Confidence: 95%

🎯 ẢNH HƯỞNG  • payment-api • cust-app • omni-agent-replay01
🌐 Lan toả: Dependent services, application, log processing pipeline

🔍 ĐÃ CHẨN ĐOÁN (4 turns)
Turn 1: ... $ df -h /mnt /var ✅ / $ systemctl is-active payment-api ✅ active
Turn 2: ... $ curl --fail http://localhost:8080/api/health ❌ rc=22
        $ nc -zv localhost 8080 ⛔ blocked: command_not_whitelisted
Turn 3: llm_error
Turn 4: Payment-api misconfigured or unreachable for specific endpoints

🛠️ CẦN LÀM
1. sudo systemctl restart payment-api  # Restart service to apply configuration changes
2. curl -v http://localhost:8080/api/health  # Verify endpoint responsiveness after restart
3. journalctl -u payment-api --since "1 hour ago"  # Check for configuration errors
```

**Giá trị: cao và thật.** Nêu đúng tên dịch vụ, chứng minh bằng lệnh **đã chạy thật trên máy**
(`curl` rc=22 là bằng chứng vật lý, không phải suy đoán), khoanh vùng lan toả, và đưa lệnh cụ thể
kèm lý do. Đây là thứ một SRE thật sẽ viết.

**Khoảng cách giữa 2a và 2b không nằm ở domain hay ở prompt — nằm ở chỗ LLM có trả lời kịp hay
không.** Cùng một đường ống, cùng một code.

---

## 3. Nguyên nhân gốc — đã đo, không suy đoán

### 3.1 LLM chỉ phục vụ 1 request tại một thời điểm

Cấu hình sống trên pod `omni-fullstack`: `OMNI_LLM_NUM_PARALLEL=1`, model `qwen3:8b`,
endpoint `http://100.93.3.96:11434/v1` (Ollama trên MacBook qua Tailscale).

Đo thật bằng cách gọi 3 request đồng thời với prompt cỡ chẩn đoán (1980 prompt token):

| Request | Thời gian trả về |
|---|---|
| req3 | 18s |
| req2 | 37s |
| req1 | 51s |

**Xếp hàng tuyến tính hoàn hảo** (~18s mỗi suất) — xác nhận không có xử lý song song.
Một lượt đơn lẻ = **24s** (đo riêng).

### 3.2 Nhu cầu vượt công suất

- Công suất: 3600s/giờ ÷ 24s/lượt = **~150 lượt/giờ**
- Nhu cầu thật: 2773 lượt ÷ 16 giờ = **~173 lượt/giờ**

Nhu cầu **≈115% công suất** ⇒ hàng đợi tăng vô hạn ⇒ vượt `llm_chat_timeout_sec=120s` ⇒
`llm_error`. Đây là lý do 74.3% lượt chết, và vì sao tỉ lệ hỏng **không tự hồi phục**.

Điều này khớp với ghi chú `diagnosis_loop.py:214` đã có sẵn trong code: *"khi LLM bắt đầu timeout
thì nó gần như không bao giờ hồi"*.

### 3.3 Hệ quả: độ trễ evidence → Telegram

Đo trên 52 trace có đủ cả mốc `EVIDENCE` và `DISPATCH`:

| | Trung vị | p90 | Max |
|---|---:|---:|---:|
| Evidence → Telegram | **8.3 phút** | **14.6 phút** | **23.2 phút** |

Với một sự cố dịch vụ, người vận hành biết từ hệ thống giám sát của họ **rất lâu** trước khi Omni
gửi tin. Ở mức trễ này, cảnh báo mất phần lớn giá trị ngay cả khi nội dung đúng.

> **Ghi nhận sai lầm trong chính phiên audit này:** tôi từng nghi các cảnh báo `service` lúc 10:54–10:58
> là "cảnh báo ma" về unit `omni-remote-agent` đã xoá ở Đ50, vì `systemctl --state=failed,activating`
> trên VM trả **rỗng**. Kiểm tra mốc `EVIDENCE` cho thấy evidence được thu lúc **10:43–10:44**, còn
> unit bị xoá lúc **10:48:47** — cảnh báo hoàn toàn THẬT, chỉ là đến muộn 10–15 phút. Giả thuyết
> "ma" sai; chính độ trễ đã tạo ảo giác đó. Ghi lại vì đây đúng là kiểu nhầm mà độ trễ cao sẽ
> tiếp tục gây ra cho người vận hành thật.

### 3.4 Cổng lọc không hề kiểm tra confidence

`workers/remote_diagnosis_emitter.py:69` — `diagnosis_has_real_finding()` chỉ kiểm tra
`root_cause` khác rỗng và không khớp regex "không có vấn đề". Chuỗi
`"Diagnosis inconclusive — no infrastructure-error-free turn produced a hypothesis"`
**thoả cả hai điều kiện** ⇒ trả `True` ⇒ thẻ được gửi.

Đã xác minh chạy thật: với session conf=0.0 ở §2a, `diagnosis_has_real_finding` = **True**.

Đây là lý do tỉ lệ gửi là **100%** thay vì ~2%. `confidence` được tính, được in vào thẻ
("Confidence: 0%"), nhưng **không được dùng để quyết định có gửi hay không**.

### 3.5 Cờ `degraded` không phản ánh sự thật

Chỉ **32/989** session có `degraded=True`, trong khi **767** có confidence 0.0 do LLM chết.
Session ở §2a có `degraded: false` dù cả 2 lượt đều `llm_error`. Cờ này không dùng được để lọc.

---

## 4. Domain không có mẫu — nêu đúng nguyên trạng

- **`kubernetes`**: có **5 trace** (`gw-prom-*`) nhưng **0 diagnosis session**. Lý do không phải
  lỗi: các trace này đến từ Prometheus và bị **cổng 3σ chặn đúng** —
  `LLM skip: 3σ gate: z_cpu=-0.4643 z_mem=-0.8815 within ±3.0σ — advisory suppressed (false
  positive)`. Toàn bộ 10 stage sau đó `skip` sạch sẽ. **Đây là phần chạy tốt nhất trong hệ thống**
  và nên là hình mẫu cho đường `ra-*`: có cổng định lượng chặn trước khi tốn LLM.
- **`hardware`**: 0 collector, 0 evidence — đúng như CLAUDE.md đã ghi, là giới hạn kiến trúc
  (agent chạy container, không truy cập được cảm biến vật lý), không phải nợ kỹ thuật.
- **`storage`/`database`/`network`/`security`**: có chạy thật nhưng 2–8 mẫu, **0–1 mẫu hữu ích**.
  Không đủ dữ liệu để kết luận chất lượng riêng của từng domain; chúng chìm chung trong vấn đề
  LLM ở §3.

---

## 5. Trả lời trực tiếp 5 câu hỏi ban đầu

| Câu hỏi | Trả lời dựa trên đo đạc |
|---|---|
| Domain đang làm gì? | 7/9 domain có collector chạy thật và thu đúng số liệu. Phần thu thập **không phải vấn đề**. |
| Trả về cái gì? | `extracted_fact` + `alert_hint` đúng cấu trúc; 1423 lệnh chẩn đoán đã chạy thật trên VM và trả kết quả về. Phần cơ khí **tốt**. |
| Xử lý ra sao? | Đúng thiết kế trên giấy (RAG→LLM→CRAT→Telegram, có audit block). Nhưng 74% lượt LLM chết vì timeout ⇒ khâu **suy luận** hỏng. |
| Telegram có gì? | 92%: "không chẩn đoán được" + 4 câu khuyên chung chung. 2.2%: chẩn đoán thật sự tốt, có bằng chứng lệnh chạy thật. |
| Có giúp người vận hành không? | **2.2% có. 97.8% không** — và phần không giúp còn gây hại vì làm nhờn cảnh báo, trễ 8–23 phút. |

---

## 6. Điều KHÔNG hỏng (để không sửa nhầm chỗ)

- Thu thập evidence trên VM: đúng, đủ, không mất.
- Định tuyến domain (`detect_domain` + `domain_hint`): chưa thấy ca nào gán sai domain trong mẫu đã xem.
- Thực thi lệnh chẩn đoán từ xa: 1423 lệnh chạy thật, có allowlist chặn đúng (`nc` bị chặn — đúng thiết kế).
- CRAT audit: mọi ca đều ghi block trước khi phát tin (fail-closed giữ đúng).
- Cổng 3σ trên đường Prometheus: chặn false-positive chính xác.
- Không mất tin: 232 tin gửi trong ~3h, chỉ **1** lần lỗi `429 Too Many Requests` từ Telegram.

---

## 7. Điều cần quyết định (KHÔNG tự sửa trong audit này)

Xếp theo tỉ lệ giá trị/chi phí, dựa trên số đo ở trên:

1. **Chặn tin vô giá trị** — thêm điều kiện confidence vào `diagnosis_has_real_finding()`.
   Sửa vài dòng, cắt ngay ~92% tin nhiễu. Rủi ro: bỏ sót ca thật mà LLM không kết luận được ⇒ nên
   gộp thành một bản tóm tắt định kỳ thay vì im lặng hoàn toàn.
2. **Giảm tải LLM xuống dưới công suất** — nhu cầu 115% công suất là gốc của mọi thứ. Ba hướng
   độc lập: (a) tăng `NUM_PARALLEL` / dùng máy mạnh hơn; (b) thêm cổng định lượng chặn trước LLM
   giống cổng 3σ đang chạy tốt ở đường Prometheus; (c) hạ tần suất probe `service_systemd_units`
   (đang chiếm 51% tải).
3. **Sửa cờ `degraded`** để phản ánh đúng lượt LLM chết — hiện không dùng được để lọc hay báo cáo.
4. Bốn rủi ro tồn đọng từ Đ50 (hardening agent chạy root, ADR-001 §5, `aoip.agent.daemon` chưa
   deploy, không có cơ chế phát hiện double-fire) vẫn nguyên, không đụng trong audit này.

---

## 8. Cách tái kiểm mọi số liệu trên

```bash
# Lấy toàn bộ diagnosis session thật
kubectl exec -n multi-agent redis-0 -c redis -- sh -c \
  'for k in $(redis-cli --scan --pattern "omni:diag:session:*"); do redis-cli GET "$k"; done' \
  > /tmp/all_sess.jsonl        # 989 dòng

# Đo xếp hàng LLM (3 request đồng thời)
for i in 1 2 3; do ( time curl -s -m 300 $OLLAMA/v1/chat/completions -d @prompt.json ) & done; wait

# Dựng lại tin Telegram thật bằng chính code hệ thống
PYTHONPATH=src .venv/bin/python -c \
 "import json;from workers.remote_diagnosis_emitter import render_diagnosis_session as r;\
  print(r(json.load(open('/tmp/sess.json'))))"

# Kiểm collector trên VM
orb -m cust-edge -u root sh -c 'cd /opt/omni-remote-agent && ./venv/bin/python -c \
 "import asyncio;from remote_agent.collectors.services import collect_systemd_units;\
  print(asyncio.run(collect_systemd_units(\"cust-edge\")))"'
```
