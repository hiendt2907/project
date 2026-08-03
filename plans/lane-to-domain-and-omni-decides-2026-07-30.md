# Kế hoạch: bỏ 4 lane → 9 domain, và Omni tự phán bất thường (tầng 1+3)

> Trạng thái: **kế hoạch, chưa sửa gì.** Quyết định của chủ hệ thống đã chốt ở
> `docs/handoffs/CURRENT_SESSION.md`. Khảo sát nền: `plans/architecture-review-2026-07-30.md`.

## 0. Ba trục cùng tên "lane" — đọc trước khi sửa một dòng nào

| trục | giá trị | bản chất | số hoá |
|---|---|---|---|
| **A** `envelope.lane` | `SYS_RESOURCE`, `SYS_HARD_FAIL`, `APP_HTTP`, `SIEM_SECURITY`, `ONBOARDING_DISCOVERY` | collector đoán lĩnh vực kỹ thuật | 29 file `src/`, 68 file `tests/` |
| **B** `proof_lane` | `state`, `resource`, `app_log`, `siem` | **loại bằng chứng vật lý cần có để mở cổng** | `pkg/reasoning/incident_matrix_profile.py:308`, `VALID_PROOF_LANES`, `evidence_consumer` 848/859, `LANE_BADGE` |
| **C** semaphore lane | `proactive`, `reactive` | pool đồng thời LLM | `llm_semaphore.py`, `metrics_exporter.py:558` |

**Chỉ trục A bị bỏ.** B và C không đổi một ký tự.

Hai bẫy trùng tên phải có hàng rào:
1. `normalize_domain("siem")` → `security`. Nếu `proof_lane` lọt vào đường chuẩn hoá
   domain, nó thành domain **hợp lệ trong im lặng**. → test hàng rào ở Phase 0.
2. `normalize_domain("resource"|"state"|"app_log")` → `unknown`. Nếu ai đó "sửa" bằng
   cách thêm alias cho ba giá trị này thì trục B bị hoà tan. → ghi cảnh báo ngay trong
   `taxonomy.py`.

## 0b. Bẫy dữ liệu: `pattern_key` nhúng lane

`advisory_pattern_key({"lane": ..., "alertname": ...})` (`workers/advisory_ack.py:152`)
là khoá của **`scope_grant`** và **`case_ledger`** (`migrations/omni_admin/0012_case_ledger.sql:13`
— `lane TEXT NOT NULL DEFAULT ''`).

⇒ Đổi giá trị lane mà không di trú `pattern_key` thì **mọi quyền khách đã duyệt ngừng
khớp, không có lỗi nào bật ra**. Omni chỉ đơn giản mất quyền. Đây là rủi ro nghiêm trọng
nhất của cả kế hoạch, và là lý do Phase 3 phải đi **sau** Phase 1–2, không song song.

## Bản kiểm kê nơi lane trục A đang sống

**Đường GHI (collector đặt lane):** `remote_agent/collectors/{system,services,database,storage,logs,k8s,api_contract,discovery_evidence}.py` — 8 file, ~19 chỗ.

**Đường ĐỌC / phân nhánh:** `workers/{evidence_consumer,os_diagnostic_loop,os_state_validator,remote_diagnosis_emitter,verify_reconcile,diagnostic_probe_registry}.py`, `pkg/reasoning/{domain_signals,evidence_cluster,schema}.py`, `pkg/{autonomy/policy,risk_taxonomy}.py`, `services/evidence_adapter/siem_adapter.py`, `aoip/{recovery,capabilities/*}.py`, `gateway/routes/{agent_push,agent_webhook,kpi,simulate}.py`, `gateway/schemas/agent_envelope.py`, `workers/schemas/playbook.py`.

**Dữ liệu đã lưu (phần "clean"):**
- PG: `case_ledger.lane`, và `pattern_key` ở `case_ledger` + `scope_grant`
- Redis: `omni:kpi:detected:{tenant}:{lane}`, `omni:kpi:resolved:{tenant}:{lane}` (tiền lệ di trú: `scripts/kpi_key_migrate.py`)
- RAG JSONL: `data/rag_training/{omni_sop_samples,sys_hard_fail_os_advisory_pairs,impact_chain_advisory_pairs,meta_self_advisory_pairs}.jsonl` → và bản đã nạp trong Redis `omni:rag:sop` (HLEN 1019)
- Prometheus: `k8s/monitor/prometheus.yaml` có nhãn lane
- Script/E2E/chaos: 20 file trong `scripts/`

---

# PHẦN I — Bỏ lane trục A, chuyển sang 9 domain

Nguyên tắc xuyên suốt: **additive trước, cắt sau.** Không có bước nào vừa thêm domain
vừa xoá lane trong cùng một commit. Lý do: agent trên máy khách nâng cấp trễ hơn Omni —
`INV` "chuẩn hoá khi ĐỌC, luôn ghi bằng canonical" của `taxonomy.py` chỉ đúng nếu đường
đọc còn hiểu payload cũ.

## Phase 0 — Cắm mốc ranh giới (không đổi hành vi)

1. `src/pkg/domain/taxonomy.py`: thêm `LANE_TO_DOMAIN` (trục A → 9 domain) và
   `lane_to_domain(lane)`:
   | lane | domain |
   |---|---|
   | `SYS_RESOURCE` | `os_host` |
   | `SYS_HARD_FAIL` | `unknown` ⚠️ **không map 1-1** |
   | `APP_HTTP` | `application` |
   | `SIEM_SECURITY` | `security` |
   | `ONBOARDING_DISCOVERY` | `unknown` (là một *pha*, không phải domain) |

   ⚠️ `SYS_HARD_FAIL` là chỗ mất thông tin thật: nó đang gánh cả `database`
   (`database.py:98,199`), `storage` (`storage.py:158,230`), `service`
   (`services.py:236`), `kubernetes` (`k8s.py:69`). **Không đoán bừa từ lane** — domain
   phải lấy từ **collector nào phát ra**, không phải từ giá trị lane. `lane_to_domain`
   chỉ dùng cho **dữ liệu lịch sử** và trả `unknown` cho `SYS_HARD_FAIL`.
2. Thêm khối cảnh báo trong `taxonomy.py`: cấm thêm alias cho `state`/`resource`/`app_log`;
   giải thích `siem` là alias domain nhưng **trùng tên** một `proof_lane`.
3. Test hàng rào `tests/test_domain_lane_boundary.py`:
   - mọi giá trị trong `VALID_PROOF_LANES` khi đi qua `normalize_domain` phải **không**
     thay đổi ý nghĩa cổng — cụ thể: khẳng định `proof_lane` không bao giờ được lấy từ
     `normalize_domain` (grep-test trên source: `resolve_proof_lane` không import taxonomy)
   - `lane_to_domain("SYS_HARD_FAIL") == "unknown"` (chống ai đó "sửa cho đẹp")
   - `normalize_domain` phủ hết 5 giá trị trục A

**Xong khi:** test mới xanh, 7023 test cũ vẫn xanh, không file nào ngoài `taxonomy.py`
+ test mới bị sửa.

## Phase 1 — Envelope mang `domain`, song song với `lane`

1. `build_envelope(...)` (`remote_agent/evidence.py`) nhận `domain: str` **bắt buộc**,
   validate bằng `require_domain` (đường ghi ⇒ ném lỗi, không im lặng). `lane` giữ
   nguyên, thành **derived + deprecated**.
2. Mỗi collector khai domain đúng của mình — đây là chỗ thu lại thông tin mà
   `SYS_HARD_FAIL` đã làm mất:
   | file | domain |
   |---|---|
   | `system.py` | `os_host` |
   | `storage.py` | `storage` |
   | `database.py` | `database` |
   | `services.py` | `service` |
   | `logs.py` | `application` |
   | `k8s.py` | `kubernetes` |
   | `api_contract.py` | `application` |
   | `discovery_evidence.py` | theo từng probe (không đồng loạt `SYS_RESOURCE` như hiện tại) |
3. `gateway/schemas/agent_envelope.py`: thêm field `domain` (optional, default suy từ
   `lane_to_domain` khi agent cũ không gửi). `agent_webhook.py`/`agent_push.py` chuẩn hoá
   khi đọc, ghi vào envelope Kafka.

**Xong khi:** envelope trên topic thật có cả `domain` và `lane`; agent bản cũ (không gửi
`domain`) vẫn chạy — chứng minh bằng test payload thiếu `domain`.

## Phase 2 — Đường đọc chuyển sang `domain`

Từng nhóm một commit, mỗi commit test xanh:
1. `pkg/reasoning/domain_signals.py` — đã là "domain" về tên gọi, đổi sang canonical.
2. `workers/{diagnostic_probe_registry,os_diagnostic_loop,os_state_validator}.py` — chọn
   probe theo domain thay vì lane. **Nối vào catalogue 99 lệnh/9 domain** đã khai ở
   `config/diagnostic_commands.yaml`: đây là lợi ích thật của cả việc bỏ lane — 5 domain
   trước đây không có cửa vào giờ có.
3. `pkg/reasoning/{evidence_cluster,schema}.py`, `pkg/risk_taxonomy.py`, `pkg/autonomy/policy.py`.
4. `workers/evidence_consumer.py` — **thận trọng nhất.** Chỉ đổi những chỗ đọc
   `ev["lane"]` trục A. **Không chạm** 848/859 (`proof_lane`), không chạm `mark_stage(..., lane=...)`
   (tham số đó mang `proof_lane`).
5. `gateway/routes/{kpi,simulate}.py` — `LANE_KEYS` của simulate thành `DOMAIN_KEYS`,
   giữ alias đường cũ để `make e2e-*` không vỡ.

## Phase 3 — Clean dữ liệu (chỉ sau khi Phase 1–2 xanh trên cluster thật)

Thứ tự có chủ đích: **quyền trước, số liệu sau, RAG cuối.**

1. **`pattern_key` (rủi ro cao nhất).** Migration `0014`: thêm cột `domain` vào
   `case_ledger`; với mỗi hàng, tính `pattern_key` mới từ domain và **giữ cả khoá cũ**
   trong cột `pattern_key_legacy`. `scope_grant` tra theo cả hai trong một cửa sổ chuyển
   tiếp. Gate xác minh: **số `scope_grant` khớp được trước và sau migration phải bằng
   nhau** — thêm vào `make verify-case-ledger` (đang có 16 gate).
2. **KPI Redis keys.** Script di trú theo mẫu `scripts/kpi_key_migrate.py`:
   `omni:kpi:*:{tenant}:{lane}` → `:{domain}`. `SYS_HARD_FAIL` gộp vào `unknown` —
   ghi rõ trong báo cáo di trú, **không phân bổ đoán** vào database/storage/service.
3. **RAG.** 4 file JSONL + `omni:rag:sop`. Đã có tiền lệ
   `scripts/rebuild_rag_from_postmortems.py`. Verify: `HLEN` sau ≥ trước (1019).
4. **Prometheus + scripts/chaos/e2e.** Đổi nhãn, chạy lại `make e2e-incident-matrix`.

## Phase 4 — Cắt lane trục A

Chỉ khi: fleet agent 3/3 đã gửi `domain`, và không còn đường đọc nào dùng lane trục A.
Xoá field khỏi `build_envelope`, giữ `lane_to_domain` cho dữ liệu lịch sử. Cập nhật
`CLAUDE.md` (bảng DIAGNOSTIC FLOWS 4 lane), `MEMORY.md`, `docs/CODEBASE.md`.

---

# PHẦN II — Omni tự phán bất thường (tầng 1 + tầng 3)

Tầng 2 (mượn Prometheus/Zabbix của khách) **để phase sau** theo quyết định của user.

## Phase 5 — Agent thôi dán nhãn, Omni giữ dòng số

**Vấn đề gốc, đã xác minh:** `collectors/system.py:53` tính
`anomaly = cpu > cpu_warn or ...` rồi tự đặt `result=FAILED`, `signal_type=ANOMALY`.
Còn `knowledge_pipeline._handle_metric_sample:87` tính z-score xong `logger.debug` — **bỏ**.
Nên người quyết định là ngưỡng tĩnh trên máy khách, và phần Omni tự học thì không có
đường ra.

1. `collect_system_metrics`: bỏ nhánh quyết định. Luôn `signal_type=METRIC_SAMPLE`,
   `result="OBSERVED"`. Vẫn gửi `thresholds` đã nhận **kèm trong fact** (`thresholds_seen`)
   để Omni biết hàng rào tĩnh là bao nhiêu — nhưng agent không dùng nó để phán.
   Áp cùng cách cho `storage.py`, `database.py`, `services.py` (các chỗ
   `lane="SYS_HARD_FAIL" if anomalies else ...`): những collector này đọc **trạng thái
   nhị phân** (unit failed, mount RO) chứ không phải ngưỡng số — chúng vẫn được báo
   `ANOMALY`, vì đó là *sự thật vật lý*, không phải một phép so ngưỡng. **Chỉ bỏ nhánh
   phán ở chỗ so số với ngưỡng.**
2. `INV_DATA_RESIDENCY`: không đổi. Luồng này chỉ có số (`cpu_percent`, `mem_percent`, …)
   — đã chạy sẵn hôm nay dưới `METRIC_SAMPLE`. Không thêm nội dung log.

## Phase 6 — `INV_KNOWLEDGE_NOT_ALERT` nới có kiểm soát

Bất biến hiện tại: non-ANOMALY ⇒ không RAG, không LLM, không alert. Nới thành:

> `METRIC_SAMPLE` **được phân tích** (baseline + phát hiện lệch, thuần số, không LLM).
> Chỉ khi phát hiện lệch mới **nâng cấp** thành `ANOMALY` và đi vào pipeline chẩn đoán
> đầy đủ (RAG + LLM). Vẫn giữ: một `METRIC_SAMPLE` bình thường **không** gọi LLM và
> **không** tạo incident.

Đổi tên bất biến cho khỏi hiểu sai: `INV_KNOWLEDGE_NOT_ALERT` → giữ tên, ghi lại định
nghĩa trong `CLAUDE.md` + docstring `knowledge_pipeline.py`.

Cài đặt trong `_handle_metric_sample`, thay `logger.debug`:
1. Đọc `get_confidence_level(redis, tenant, host)` — thang có sẵn, chưa được dùng để
   quyết định gì.
2. Quyết định theo **tầng 3 (cold-start guard)**:
   | confidence | ai phán |
   |---|---|
   | `STATIC_GUARD` (0–24) | hàng rào tĩnh, đọc từ `resolve_agent_thresholds` **ở Omni** — không phải ở agent |
   | `LEARNING` (25–49) | hàng rào tĩnh, + ghi lệch z-score vào sổ để đối chiếu (chưa nâng ANOMALY) |
   | `ASSISTED` (50–74) | z-score là chính; hàng rào tĩnh thành cận trên |
   | `AUTONOMOUS` (75–100) | chỉ z-score |
3. Khi phán là lệch: `kafka.send_dict` vào `omni-diagnostic-evidence` với
   `signal_type=ANOMALY`, `domain` giữ từ envelope gốc, và **ghi rõ nguồn phán**
   (`decided_by=omni_baseline` / `omni_static_guard`, kèm z-score và confidence).
   `knowledge_pipeline.py:184` đã có sẵn `kafka.send_dict` — không cần hạ tầng mới.
4. Chống lụt: khoá dedup `omni:knowledge:promoted:{tenant}:{host}:{metric}` TTL ~600s.
   Nếu thiếu bước này, một host CPU cao liên tục sẽ bơm một ANOMALY mỗi chu kỳ agent.

## Phase 7 — Vòng ReAct lên đúng đường bằng chứng

Hiện `agentic_slow_path_with_llm_and_tools` chỉ chạy từ `handle_inbound_payload` (đường
**chat**). Nối nó vào đường bằng chứng cho ANOMALY vừa được Omni nâng cấp, với hai hàng rào:
- read-only: chỉ lệnh trong catalogue chẩn đoán của **tenant đó** (nối sang Phần III đã
  chốt: per-tenant + bộ mặc định) — không phải mutation, nên `shadow` vẫn hợp lệ.
- ngân sách: giới hạn số bước và số lệnh mỗi trace, đếm được, ghi CRAT.

## Phase 8 — Chứng minh trên hệ thật, không chỉ test

Bài học đã ghi: *test tổng hợp xanh không chứng minh năng lực chạy được.*
1. `hasattr()` / `import` thật trong pod `omni-fullstack` cho từng module mới.
2. Trên VM lab: bơm tải CPU thật → khẳng định (a) agent gửi `METRIC_SAMPLE` **không**
   dán nhãn, (b) Omni nâng cấp thành ANOMALY, (c) `decided_by` ghi đúng nguồn,
   (d) thẻ Telegram lên với domain đúng.
3. Kiểm ngược: host bình thường ⇒ **không** ANOMALY nào, và không LLM call nào — chống
   biến kế hoạch này thành máy sinh cảnh báo.

---

## Thứ tự thực hiện và điểm không thể quay lại

```
Phase 0 → 1 → 2 → (5 → 6 → 7 song song được với 3) → 3 → 4 → 8
```
- Phase 0–2 **có thể quay lại** (additive).
- **Phase 3 là điểm không quay lại** — di trú `pattern_key`. Bắt buộc dump PG trước.
- Phase 4 (cắt lane) đi cuối cùng, sau khi fleet agent đã cập nhật.

## Những gì kế hoạch này CỐ Ý không làm

- Không chạm `proof_lane` (trục B) và semaphore lane (trục C).
- Không map `SYS_HARD_FAIL` sang một domain cụ thể cho dữ liệu lịch sử — `unknown` là
  câu trả lời trung thực; đoán bừa rồi dùng để cấp quyền tệ hơn thừa nhận chưa biết.
- Không gửi nội dung log lên Omni (tầng 2 của đề xuất Q2 để phase sau).
- Không thiết kế lại taxonomy domain hay catalogue lệnh — đã chốt, đã verify trên cluster.
