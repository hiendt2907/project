# Trạng thái TRƯỚC / TẠI MỐC ĐÓNG BĂNG / SAU — dự án PROACTIVE-first

**Mốc đo BEFORE:** commit `bae4df8` · branch `main` · working tree sạch · `2026-08-09T10:32Z`
**Cụm đo:** GCP `omni-k3s-vm`, namespace `multi-agent`, pod `omni-fullstack-56cf847fc7-7jqd6`
(khởi động `2026-08-09T06:02:22Z`).

> **Luật của tài liệu này.** Mỗi ô phải kèm LỆNH đã chạy hoặc `file:dòng`. Ô nào không đo được
> ghi đúng chữ **KHÔNG ĐO ĐƯỢC** kèm lý do — cấm để trống, cấm ước lượng, cấm suy từ tên hàm
> hay từ comment. Cột 2 và 3 để trống CÓ CHỦ ĐÍCH, điền ở P3 và P6 bằng **đúng những lệnh này**.
> BEFORE phải commit trước khi sửa dòng code đầu tiên.

---

## Bảng chỉ số

| # | Chỉ số | TRƯỚC GOM (bae4df8) | TẠI MỐC ĐÓNG BĂNG | SAU GIGO |
|---|---|---|---|---|
| 1 | Nguồn khởi phát chẩn đoán ở tầng loop | **10** (5 reactive + 5 proactive) | | |
| 2 | Trong đó kết thúc bằng vòng chẩn đoán thật | **4** + 1 vòng ReAct riêng không qua `run_diagnostic_pipeline` | | |
| 3 | Điểm vào HTTP độc lập bơm cùng topic | **6 route** | | |
| 4 | Call site quyết định urgency/severity | **28** (16 Omni + 12 collector trên host khách) | | |
| 5 | Cặp cài đặt trùng lặp | **6** (D1–D6) | | |
| 6 | LLM call/giờ — tổng | **6,73/h** (30 call / 4,459h) | | |
| 7 | LLM call/giờ — steady-state | **3,36/h**, nhịp ~630s | | |
| 8 | Thời lượng 1 call LLM min/trung vị/max | **10,5s / 34,4s / 120,0s** | | |
| 9 | Trace khởi nguồn PROACTIVE trong cửa sổ 4,459h | **0** | | |
| 10 | Trace khởi nguồn ALERT trong cùng cửa sổ | **26** (`gw-prom-*`) | | |
| 11 | Trace có `domain` khác rỗng (traffic sống) | **0 / 100%** | | |
| 12 | Trace có `signal_kind` khác rỗng (traffic sống) | **0 / 100%** | | |
| 13 | Call site `detect_domain()` thiếu `domain_hint` | **0 / 2** | | |
| 14 | Ngưỡng cắt bằng chứng đưa vào LLM | **~10 000 chars, giữ ĐUÔI** | | |
| 15 | MTTD | **KHÔNG ĐO ĐƯỢC** | | |
| 16 | Tổng dòng Python trong `src/` | **98 587** (474 file) | | |
| 17 | Dòng của 21 file thuộc đường xử lý sự cố | **15 217** (≈15,4%) | | |

---

## Bằng chứng

### 1–3. Topology đường khởi phát

Nguồn: `grep -n "create_task" src/workers/omni_worker.py` (26 call site) rồi truy từng loop.

**REACTIVE (5):** `kafka_alerts_loop` (`omni_worker.py:642`, đăng ký `:1219`) → `run_diagnostic_pipeline`
· `kafka_evidence_loop` (`:685`, `:1233`) → `reason_from_diagnostic_evidence` → `run_diagnosis_loop`
· `kafka_knowledge_evidence_loop` (`:844`, `:1265`) → `_promote_to_anomaly` → nạp lại đường evidence
· `kafka_siem_chains_loop` (`:1017`, `:1258`) · `kafka_siem_correlation_loop`
(`siem_correlation_loop.py:70`, `:1262`) → nạp vào siem-chains.

**PROACTIVE (5):** `kafka_proactive_incidents_loop` (`proactive_observer.py:957`, `:1289`) →
`run_diagnostic_pipeline` · `proactive_evaluate_loop` (`:1013`, `:1288`) ·
`autonomous_decider_loop` (`autonomous_decider.py:714`, `:1286`) — vòng ReAct **riêng**, không đi
qua `run_diagnostic_pipeline` · `autonomous_forecast_loop` (`forecast_autonomous_loop.py:160`,
`:1282`) · `deep_scout_periodic_loop` (`deep_scout.py:434`, `:1281`).

**6 điểm vào HTTP** (`grep -rn "send_and_wait|send_dict" src/gateway/`): `POST /webhook/prometheus`
(`api.py:671`) · `POST /webhook/agent/evidence` (`agent_webhook.py:470`) · `POST /agent/v1/push`
(`agent_push.py:178`) · `POST /simulate/{lane}` (`simulate.py:599`) · `POST /simulate/scenario/{s}`
(`simulate.py:503`) · `POST /api/gateway/diagnostics/test` (`diagnostic.py:158`).

**Cổng bật/tắt — giá trị HIỆU LỰC trong pod** (`kubectl exec ... -- printenv | grep ^OMNI_`,
131 biến được set):

| Cổng | Giá trị sống | Nguồn |
|---|---|---|
| `OMNI_AUTONOMOUS_DECIDER_ENABLED` | **false** | env pod (default code cũng `False`) |
| `OMNI_SIEM_CORRELATION_ENABLED` | **true** | env pod (default code `False`) |
| `OMNI_TELEGRAM_POLLING_ENABLED` | **false** | env pod |
| `proactive_enabled` | **true** | không set ⇒ default `settings.py:1304` |
| `siem_chain_consumer_enabled` | **true** | không set ⇒ default `settings.py:669` |

⇒ Vòng ReAct proactive (`autonomous_decider`) **đang TẮT** trên GCP.

### 4. 28 call site quyết định urgency

**16 phía Omni:** `domain_signals.py:337` (`assess_domain_severity`) · `remote_triage.py:66/102/140`
· `agent_webhook.py:248` · `knowledge_pipeline.py:149/229` · `remote_host_baseline.py:62` ·
`three_sigma.py:123` · `alert_qos.py:107` · `siem_bridge.py:120` · `siem_adapter.py:64` ·
`reason_codes.py:59` · `analyst_advisory_schema.py:207` · `unified_incident_card.py:136` ·
`question_lifecycle.py:124`.

**12 phía collector trên host khách** (`grep -rn '"FAILED"' src/remote_agent/collectors/`, đều là
ngưỡng tĩnh hardcode): `services.py:132/156/308` · `database.py:103/155/206/240` · `logs.py:132` ·
`storage.py:157/227` · `network.py:125` · `k8s.py:61`.

### 5. Sáu cặp trùng lặp

| | Cặp | Bằng chứng |
|---|---|---|
| D1 | `/simulate/scenario/*` vs `/api/gateway/diagnostics/test` | Cùng 4 key `service/network/disk/cpu`, cùng probe, cùng envelope, cùng topic (`simulate.py:503` / `diagnostic.py:158`). Cả hai đều mount (`api.py:487`, `:490`) |
| D2 | `siem_bridge.translate_incident` vs `SIEMEvidenceAdapter.to_evidence` | `SEVERITY_MAP` nhân đôi (`siem_bridge.py:71` / `siem_adapter.py:25`), `CATEGORY_TO_ALERTNAME` nhân đôi, cùng `trace_id = f"fg-{incident_id[:8]}"` |
| D3 | `POST /agent/v1/push` vs `POST /webhook/agent/evidence` | Helper trùng từng dòng (`agent_push.py:38-49` / `agent_webhook.py:258-270`); **hai mặt phẳng xác thực song song** cho cùng loại dữ liệu (`api.py:491` ghi rõ "no gateway API key guard") |
| D4 | 2 emitter Telegram + 1 wrapper thứ ba | `telegram_advisory_emitter.py:44-46` / `remote_diagnosis_emitter.py:303-307` / `telegram_outbound.py:32-57` — ba bản của cùng timeout-wrapper |
| D5 | 2 đường phê duyệt HITL + 1 hệ ack riêng | `hitl_telegram.py:229` vs `tier_loops.py:214` (comment `:217` tự xác nhận "Song song đường Telegram"); `advisory_ack.py:1-8` là hệ thứ ba |
| D6 | 2 bảng chấm nâng cấp anomaly | `agent_webhook.py:248-274` (ở gateway) vs `knowledge_pipeline.py:149/229` (ở worker) |

### 6–8. Tải LLM

Nhận diện 1 call = 1 dòng `event=llm_call` từ `pkg.observability.llm_observability`
(`record_llm_call`, chỉ gọi từ `src/llm/vllm_client.py` — biên duy nhất ra Ollama).
Cửa sổ: toàn vòng đời pod, `1786255353` → `1786271406` = **16 053s = 4,459h**.

Đối chứng độc lập bằng counter Prometheus trong pod (khớp tuyệt đối 30 = 7 + 23):
```
kubectl exec -n multi-agent deploy/omni-fullstack -c omni-fullstack -- curl -s localhost:9090/metrics
omni_llm_calls_total{call_kind="chat",model="qwen3:8b",outcome="ok"}       7.0
omni_llm_calls_total{call_kind="structured",model="qwen3:8b",outcome="ok"} 23.0
```

| Nguồn | n | min | trung vị | max |
|---|---|---|---|---|
| `deep_scout_autonomous` (boot) | 7 | 10 458 ms | 11 224 ms | 64 966 ms |
| `diagnosis_loop` (`sim-*`, boot) | 8 | 85 554 ms | 103 118 ms | 120 046 ms |
| `advisory_analyst` (`gw-prom-*`) | 15 | 34 040 ms | 34 276 ms | 64 018 ms |

Tải là **bimodal**: 15/30 call dồn trong 5,3 phút đầu (boot burst), sau đó chỉ còn 1 nhịp advisory
mỗi ~630s. Có **một khoảng chết 6 928s (1,92h)** không call nào dù alert vẫn vào — do gate chặn
trước LLM: `advisory_sigma_gate_blocked` 11 lần, `advisory_sigma_stale` 14 lần, trên 26
`alert_kafka_in`. Outcome `ok` 30/30, 0 lỗi. Model duy nhất `qwen3:8b`.

### 9–10. Proactive vs alert

`kubectl exec -n multi-agent redis-0 -c redis -- redis-cli XRANGE omni:trace:events - +`
→ 2 031 entry, span `1785976764` → `1786271406` = 81,85h (stream `MAXLEN=2000`, đây là phần còn giữ).

Phân biệt nguồn bằng tiền tố `trace_id`, đối chiếu nơi sinh: `gw-prom-` (`api.py:148`, alert) ·
`proact-` (`proactive_observer.py:431`) · `ra-` (`remote_agent/evidence.py:48`) · `sim-` (inject tay).

| Nguồn | Toàn stream 81,85h | Cửa sổ pod 4,459h |
|---|---|---|
| `gw-prom` (alert) | 75 | **26** |
| `proact` (proactive) | **1** | **0** |
| `ra` (agent telemetry) | 808 (một burst discovery duy nhất) | 0 |
| `sim` (inject tay) | 13 | 4 |

**100% trace đi qua LLM trong 4,459h đều khởi nguồn từ alert.** Lý do proactive im, đọc từ env pod:
```
OMNI_PROACTIVE_EVAL_INTERVAL_SEC=300
OMNI_PROACTIVE_PROMQL=sum(kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"})
OMNI_PROACTIVE_TRIGGER_THRESHOLD=0
```
Vòng chạy mỗi 300s nhưng điều kiện kích hoạt là **đã có pod CrashLoopBackOff** — bản thân nó đã là
hậu quả, không phải tín hiệu sớm. Đây là lý do gốc khiến hệ "có hạ tầng proactive" nhưng vận hành
thực tế vẫn 100% reactive.

### 11–12. `domain` / `signal_kind` rỗng trên toàn bộ traffic sống

```
redis-cli --scan --pattern 'omni:trace:stages:*'   → 6 key (TTL 3600s)
redis-cli HGET omni:trace:stages:gw-prom-2c75571b6d6f __meta__
{"domain": "", "signal_kind": "", "trace_id": "gw-prom-2c75571b6d6f", ...}
```
4/4 key kiểm được đều rỗng cả hai trường. Nguyên nhân đọc từ code:
`grep -rn 'signal_kind="' src` → **32 hit, toàn bộ nằm ở `remote_agent_pipeline.py`,
`onboarding_pipeline.py`, `simulate.py`, `diagnostic.py`. KHÔNG hit nào ở nhánh alert in-cluster.**
`evidence_consumer.py:2364` chỉ khai `domain` (từ `ev_doc.get("domain")` chuẩn hoá), không khai
`signal_kind`; với alert Prometheus thì `ev_doc` không mang `domain` nên chuẩn hoá ra rỗng.

⇒ Việc gắn `domain`/`signal_kind` ở Đ38/Đ39 mới phủ **đường remote-agent**; **đường alert — tức
100% traffic sống — chưa được phủ.** Đây đúng là mục "chưa verify sống" đã khai ở Đ39, nay đo được.

### 13–14. Chất lượng đầu vào LLM (GIGO)

**Sạch, không cần sửa:**
- `detect_domain()` có **2/2** call site truyền `domain_hint` (`remote_agent_pipeline.py:232-235`,
  `agent_webhook.py:244-247`). 5 hit còn lại của grep là comment. Thiếu = 0.
- Catalogue lệnh **fail-closed thật ở tầng LOAD**: `command_catalog.py:293/309/324/194/201/205/218/228`
  đều `raise CatalogError`; consumer `validator.py:132-140` cache cả lỗi rồi **re-raise**. Resolve
  cả hai layout (`_default_catalog_candidates()` `:120-123`) và cả hai định dạng (`:122`,
  `_parse()` `:255-270` fallback JSON khi `ImportError`). Bundle sinh `.json` thật
  (`scripts/omni-agent-bundle.sh:83`).
- Hàng rào chống bịa còn sống, chặn **trước** audit + Telegram: `has_placeholder_parroting` +
  `diagnosis_has_real_finding` (`remote_diagnosis_emitter.py:63-81`, gọi ở
  `remote_agent_pipeline.py:531/535`) · `_apply_grounding_gate` ở **cả hai lối ra**
  (`diagnosis_loop.py:972` và `:1048`) · `apply_advisory_grounding_gate`
  (`advisory_analyst_handler.py:422`, chạy trước khi tính tier).
- Test: `pytest tests/test_diagnostic_catalog.py tests/test_advisory_prompt_budget.py
  tests/test_advisory_grounding_gate.py tests/test_remote_diagnosis_emitter_guards.py
  tests/test_diag_grounding_and_scope.py tests/test_diagnostic_catalog_unification.py -q`
  → **184 passed**.

**Vấn đề thật:**
- **Bằng chứng bị cắt MẤT ĐẦU.** `advisory_analyst_handler.py:304` gọi
  `truncate_for_llm(evidence_text, ~10 000, tail=True)`, và `llm_context_budget.py:69` là
  `return t[-max_chars:]`. Đầu chuỗi — alert gốc + fact sớm nhất — bị bỏ. Prompt thực đo:
  13 927 chars / 3 886 token, ổn định gần như bất biến qua 15 call.
- **Cổng bằng chứng của tool mutate.** `MUTATE_TOOL_ALLOWLIST` (`risk_taxonomy.py:200`) có 10 tool
  khai `required_evidence`, riêng **`kubectl_cluster` KHÔNG có** (`kubectl_cluster.py:88` không
  truyền `metadata=`). Điểm thực thi duy nhất là `_planner_missing_preconditions()`
  (`evidence_consumer.py:993-1090`), và `plan_origin.startswith("deterministic")` bỏ qua toàn bộ
  kiểm này một cách có chủ đích (`:1814-1818`).
  **Mức độ thật: TIỀM ẨN, chưa khai thác được** — env sống có `OMNI_AUTO_EXECUTE_ENABLED=false`.
- **Lỗi nhẹ (dọn):** `advisory_analyst_handler.py:288-289` và `proactive_observer.py:302` inline
  `getattr` thay vì `build_llm_options()`, trái quy ước trong CLAUDE.md.
  *Đã kiểm và BÁC BỎ giả thuyết "num_ctx rơi về 4096"*: `settings.py:1812` default Pydantic là
  `8192` kiểu `int` nên nhánh fallback `4096` không bao giờ chạy. Không có halving.

### 15. MTTD — KHÔNG ĐO ĐƯỢC

Ba bằng chứng độc lập:
1. `omni:incident:ts:*` chỉ ghi thời điểm evidence **tới consumer** (`evidence_consumer.py:2370`),
   không có mốc sự cố bắt đầu để trừ. 11 key, đều 1 giá trị epoch đơn.
2. `observe_kpi_mttd` là **code chết** — `grep -rn "observe_kpi_mttd" src/` chỉ trả về đúng dòng
   định nghĩa `metrics_exporter.py:1076`, không call site nào. Xác nhận trên pod: histogram
   `omni_kpi_mttd_seconds` chỉ có HELP/TYPE, không series.
3. `redis-cli --scan --pattern "omni:kpi*" | wc -l` → **0** (trong khi `DBSIZE`=18 030). Vì
   `record_detected`/`record_resolved` (`kpi_metrics.py:198-204`) chỉ chạy khi feedback có
   `outcome in ("success","APPROVED","verified")`, mà pipeline đang suggest-only nên mọi trace kết
   thúc `EXECUTOR=skip`, `FEEDBACK=skip`.

**Cần gì mới đo được:** (a) persist `startsAt` của Alertmanager cạnh `omni:incident:ts:{trace}` —
giá trị này ĐÃ được nhận ở `gateway/api.py:89` và `workers/memory/initial_symptom.py:90` nhưng
chưa từng được lưu; (b) gắn call site cho `observe_kpi_mttd`; (c) với đường proactive/agent (không
có `startsAt`), cần mốc mẫu đo vượt ngưỡng đầu tiên — hiện không ghi ở đâu.

---

## Drift tài liệu phát hiện khi đo

`CLAUDE.md` mục "Kill-switch — effective value" mô tả **lab OrbStack**, không phải GCP. Env hiệu
lực trong pod GCP:

| Biến | CLAUDE.md ghi | Thực tế GCP |
|---|---|---|
| `OMNI_AUTO_EXECUTE_ENABLED` | `true` | **false** |
| `OMNI_SIEM_SUGGEST_ONLY` | `false` | **true** |
| `OMNI_TELEGRAM_POLLING_ENABLED` | `true` (drift đã biết) | **false** |
| `OMNI_AUTO_ROLLBACK_ENABLED` | `true` | **không tồn tại** |
| `OMNI_LAB_AUTO_EXECUTE_AGENTS` | 3 agent lab | **không tồn tại** |

---

## Ba sự thật quan trọng nhất rút ra từ BEFORE

1. **Hệ đang vận hành 100% reactive dù có đủ 5 vòng proactive.** Không phải vì thiếu code, mà vì
   điều kiện kích hoạt proactive là `CrashLoopBackOff > 0` — một hậu quả, không phải tín hiệu sớm.
   Sửa đúng chỗ này là sửa **một biến cấu hình + một truy vấn**, không phải xây thêm module.
2. **Trục `domain`/`signal_kind` chưa chạm được traffic thật.** Đã phủ đường remote-agent nhưng
   đường alert — nơi có toàn bộ 26/26 trace sống — vẫn rỗng cả hai trường.
3. **Không có ground truth để chấm điểm.** MTTD không đo được, KPI store rỗng, `observe_kpi_mttd`
   là code chết. Mọi tuyên bố "tốt hơn" sau này đều vô nghĩa cho tới khi mục 15 được đóng.

---

## Chưa đo được (khai để không ai tưởng đã phủ)

- **Phần dòng code "riêng phần chẩn đoán" trong mỗi file.** Con số 15 217 là TOÀN BỘ 21 file;
  `evidence_consumer.py` (3 583 dòng) trộn nhiều trách nhiệm. Tách chính xác cần phân tích AST.
- **D6 là trùng lặp thật hay phân tầng có chủ đích.** Hai bên tính điểm trên input khác nhau
  (gateway theo item, worker theo z-score) — chỉ đọc code không kết luận được.
- **Tỉ lệ LLM chọn ĐÚNG bộ công cụ chẩn đoán.** Chưa có tập sự cố mẫu có nhãn để chấm. Đây là
  chỉ số GIGO cốt lõi ở P6 — phải dựng tập mẫu trước, nếu không thì "GIGO tốt hơn" là lời nói suông.
