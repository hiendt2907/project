# Kế hoạch: gộp FinGuard thành tính năng Smart SIEM nội bộ của Omni

**Ngày:** 2026-08-04 · **Cơ sở:** khảo sát code + cluster thật (không suy đoán)
**Quyết định của user (2026-08-04):** nguồn = security collector trên Remote Agent · chỉ bỏ policy
legacy-FinGuard · xoá hẳn 3 deployment, gộp vào `omni-fullstack` · giữ `INV_DATA_RESIDENCY`

---

## 0. Phát hiện quyết định — đọc trước khi làm bất cứ gì

> **SIEM không bị policy chặn. Nó không có dữ liệu vào.**

| Thành phần | Trạng thái thật (đã kiểm) |
|---|---|
| Namespace `finguard-customer` | **KHÔNG TỒN TẠI** trong cluster |
| `omni-siem-bridge` / `omni-hitl-dispatcher` / `omni-evidence-adapter` | **0/0 replicas**, cả 3, 109 ngày |
| Redis `corr:*` | **0 key** — correlation engine chưa từng chạy với dữ liệu thật |
| Topic `omni-siem-raw` | Tồn tại; **producer duy nhất** là `siem_bridge` (scaled-0, đọc Redis của namespace đã xoá) |
| Topic `omni-siem-incidents` | **Không tồn tại** trong Kafka dù script tạo topic có khai |
| Collector security trên Remote Agent | **Không có file nào** — không auth.log, không lastb, không sshd |

Engine xử lý đã xây **đủ và tốt**: `siem_correlation` (Python port brain-go, parity PASS) →
`omni-siem-chains` → `ChainConsumer` → advisory → `PlaybookMatcher` → CRAT. Toàn bộ chuỗi này
đang chờ một đầu vào không bao giờ đến.

⇒ Đây là lý do domain `security` trong bảng 9-domain (`CLAUDE.md`) vẫn là ❌.
⇒ **Gỡ policy không làm thêm một sự cố nào được xử lý.** Việc cần làm là **dựng đầu vào**.

---

## Phase S0 — Dọn FinGuard-như-hệ-ngoài (không rủi ro, làm trước)

Mọi thứ dưới đây đang trỏ tới một hệ thống không còn tồn tại. Đây là phần "gộp lại cho đúng".

### S0.1 Xoá 3 deployment chết + manifest
- `k8s/deployments/omni-siem-bridge{,-production}.yaml` · `omni-hitl-dispatcher{,-production}.yaml`
  · `omni-evidence-adapter{,-production}.yaml` · `finguard-customer-netpol.yaml`
- `kubectl delete deployment` cho cả 3 (kèm PDB/Service orphan nếu có)
- Gỡ target `make deploy-siem-stack`; gỡ ingress/netpol rules trỏ `finguard-customer`
- **`omni-hitl-dispatcher` nay THỪA THẬT**: HITL nội bộ đã dựng xong ở `#27`
  (`hitl_telegram.open_hitl_pending_for_mutate` → Telegram + Postgres `hitl_decision`). Không mất
  năng lực nào khi xoá.

### S0.2 Xoá/port code gọi API FinGuard bên ngoài
- `src/workers/hitl_dispatcher.py` — **xoá** (thay bởi HITL nội bộ `#27`)
- `src/workers/siem_bridge.py` — **xoá** (đọc Redis namespace đã chết; thay bởi S2)
- `src/gateway/routes/playbooks.py:20,95-127` — đang forward approve/reject tới
  `hitl-api.finguard-customer.svc.cluster.local` ⇒ **502/504 chắc chắn**. Chuyển sang
  `AdminConfigRepo.decide_hitl()` nội bộ (đã hoạt động thật sau `#27`)
- `src/services/evidence_adapter/` — giữ `siem_adapter.py` (logic chuyển đổi vẫn đúng, tái dùng ở
  S2), xoá `worker.py` (Deployment riêng đã xoá)

### S0.3 Bỏ policy legacy-FinGuard (đúng phạm vi user đã chốt)
| Policy | Vị trí | Xử lý |
|---|---|---|
| `siem_source == "finguard"` mới match playbook | `services/playbook/matcher.py:102` | **Bỏ gate** — chấp nhận mọi `siem_source`; giá trị canonical mới là `omni_siem` |
| `OMNI_SIEM_SUGGEST_ONLY` (default True) | `settings.py:1523`, `evidence_consumer.py:1346,2671` | **Bỏ hẳn** — vốn chỉ tồn tại vì SIEM đến từ hệ ngoài không kiểm soát được. SIEM nay là dữ liệu nội bộ, đi chung ma trận `tier × risk` như 8 domain còn lại |
| `siem_source=="finguard"` chọn nhánh HITL | `evidence_mutate_emit.py:291` | Đổi theo `siem_source` canonical mới |
| Chuỗi `"finguard"` trong docstring/mô tả | `settings.py:1527`, `siem_correlation/__init__.py` | Đổi tên thành Smart SIEM nội bộ |

**GIỮ NGUYÊN (user đã chốt):** CRAT fail-closed · blast-radius · `INV_NAMESPACE_ISOLATION` ·
HITL cho HIGH-risk · `INV_DATA_RESIDENCY`.
Lý do kỹ thuật, không phải thủ tục: CRAT là **nguồn nhãn duy nhất** của vòng học vừa dựng ở
`#27`/`#28` — bỏ nó thì mất luôn khả năng tự học mà `/goal` đang đòi.

### S0.4 Cập nhật tài liệu
`CLAUDE.md` (mục DEPLOYMENT STATE đang ghi 3 deployment này là "scaled-down-intentional, KHÔNG
xoá" — claim đó **hết hiệu lực** sau S0.1) · `docs/CODEBASE.md`.

---

## Phase S1 — Security collector trên Remote Agent (mở đầu vào thật)

Đây là phần làm cho domain `security` từ ❌ thành ✅ — bằng dữ liệu thật trên VM lab.

### S1.1 `src/remote_agent/collectors/security.py`
Theo đúng khuôn 6 collector đang có (`services.py` làm mẫu): async, `exec_guard.check()` trước mọi
lệnh, read-only tuyệt đối, `build_envelope(...)` với `domain=SECURITY`.

Probe đề xuất (đều là lệnh read-only có sẵn trên mọi distro):
| Probe | Nguồn | Bắt được gì |
|---|---|---|
| `security_auth_failures` | `journalctl _COMM=sshd`, `lastb` | brute-force SSH, đăng nhập sai lặp |
| `security_privilege_escalation` | `journalctl _COMM=sudo`, `/var/log/auth.log` | sudo bất thường, `su` thất bại |
| `security_account_changes` | `getent passwd/group` so snapshot trước | user/group mới xuất hiện |
| `security_listener_delta` | tái dùng `network.py` | cổng lạ mở ra (đã có, chỉ gắn nhãn security) |

**Ngưỡng đặt ở đâu**: theo `CLAUDE.md`, 5/6 domain hiện cho agent tự tính verdict thô. Nhưng
`security` nên theo mẫu `os_host` — agent gửi `result="OBSERVED"`, **Omni phán** bằng baseline nó tự
học. Lý do: ngưỡng "bao nhiêu lần đăng nhập sai là tấn công" phụ thuộc baseline từng host, hardcode
trên agent là quay lại đúng cái bẫy ngưỡng tĩnh.

### S1.2 Tôn trọng `INV_DATA_RESIDENCY` — thiết kế then chốt
Agent **chuẩn hoá ngay trên host khách** thành chuỗi `key=value` khớp allowlist có sẵn của
`siem_correlation/entities.py:36` (`user=` · `session=` · `host=` · `pod=` · `process=`), rồi gửi
**chuỗi đã chuẩn hoá + metadata**. Trường `Incident.raw_log` (`models.py:67`) **để rỗng**.

Vì sao cách này gọn: `extract_entities()` vốn đã được thiết kế allowlist-only và parse `key=value` —
**không phải sửa một dòng nào trong engine correlation**. Nội dung log thô không bao giờ rời host
khách; thứ đi lên Omni chỉ là entity đã được trích và bảng metadata.

### S1.3 Đăng ký + phát hành
`src/remote_agent/agent.py:47-61` thêm import + vòng chạy. Phát bundle lên 3 VM lab qua chính cơ
chế safe-update đã có (`IT-5`, đã drill PASS) — không copy tay.

---

## Phase S2 — Ingress nội bộ: agent → `omni-siem-raw`

Thay chỗ `siem_bridge` vừa xoá. Evidence `domain=security` từ agent đã đi qua Gateway
(`/webhook/agent/evidence`) vào `omni-diagnostic-evidence` như mọi domain khác; cần rẽ nhánh sang
`omni-siem-raw` để engine correlation nhận được.

- Định tuyến tại `agent_webhook.py` (đúng chỗ routing hiện tại — theo `INV_KNOWLEDGE_NOT_ALERT`,
  routing ở gateway chứ không ở worker)
- Tái dùng `services/evidence_adapter/siem_adapter.py` để dựng envelope `Incident` (logic chuyển đổi
  vẫn đúng, chỉ đổi nguồn), đặt `siem_source="omni_siem"`, `raw_log=""`
- Tạo topic `omni-siem-incidents` còn thiếu (`scripts/kafka_ensure_omni_topics.sh` đã khai nhưng
  topic chưa tồn tại thật)

---

## Phase S3 — Bật correlation + kiểm chứng bằng sự cố THẬT

`OMNI_SIEM_CORRELATION_ENABLED=true` đã bật sẵn trên `omni-fullstack` — nhưng chưa từng có input.

**Drill thật trên VM lab** (không nhận "test pass" làm bằng chứng):
```
ssh sai mật khẩu N lần vào cust-edge  →  collector bắt được
  →  gateway rẽ omni-siem-raw  →  corr:* có key (hiện 0)
  →  omni-siem-chains có message  →  ChainConsumer sinh advisory
  →  CRAT ghi block  →  case_ledger mở ca (nhờ #28)
  →  Telegram có nút phản hồi  →  bấm → verdict vào sổ + KPI (nhờ #29)
```
**Nghiệm thu = truy vấn được từng mắt xích trên Redis/Postgres thật.**

---

## Phase S4 — Đóng vòng học cho SIEM

Sau S3, domain `security` dùng chung hạ tầng học vừa dựng — không cần code riêng:
`case_ledger` mở ca · verdict người vào `case_verdict_history` · KPI có mẫu ±
· `advisory_promoter` cộng điểm graduation theo `pattern_key` của chính domain security
· RAG ingest có nhãn (Phase D của plan `close-incident-loop-and-rag-2026-08-04.md`).

Kiểm tra riêng: `PlaybookMatcher` sau khi bỏ gate `finguard` có match được playbook security thật
không, hay bảng `omni_admin.playbook` (hiện 0 dòng) cần seed trước.

---

## Thứ tự & phụ thuộc

```
S0 (dọn, không rủi ro)  ──→  S1 (collector)  ──→  S2 (ingress)  ──→  S3 (drill thật)  ──→  S4 (học)
   │
   └─ S0 làm được ngay, độc lập, không chờ gì
```

- **S0 trước**: dọn hết đường chết rồi mới nối đường mới, tránh 2 hệ song song như bài học brain-go
- **S3 là cổng nghiệm thu thật** — trước S3 mọi thứ chỉ là "code chạy", chưa phải "sự cố được xử lý"
- **Không bật auto-execute cho SIEM ở S3** — để một chu kỳ có nhãn thật rồi mới xét (user đã chốt
  giữ tier gate)

## Rủi ro đã biết

| Rủi ro | Giảm thiểu |
|---|---|
| Xoá `hitl_dispatcher` làm mất đường duyệt | Không — HITL nội bộ `#27` đã deploy sống và verify |
| Collector security đọc nhầm dữ liệu nhạy cảm | S1.2: chuẩn hoá + trích entity **trên host**, `raw_log` rỗng |
| Bỏ `SIEM_SUGGEST_ONLY` mở quá tay | Tier gate + blast-radius + HITL HIGH-risk vẫn giữ nguyên |
| `lastb`/`journalctl` khác nhau giữa distro | `exec_guard` + fallback nhiều nguồn, giống `pkg_origin.py` đã làm |

## Kỷ luật giữ nguyên
Scope Freeze · 1 commit = 1 concern · verify sống bằng `kubectl exec` chứ không tin "rollout
successful" · thấy lỗi ngoài scope thì log task, không tự sửa im lặng.
