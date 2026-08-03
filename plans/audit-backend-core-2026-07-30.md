# Audit backend core 2026-07-30 — 6 subagent + tự kiểm

## MỤC TIÊU USER ĐẶT RA (2026-07-30, trích nguyên văn)

> "Cái tôi nhắm đến là 1 hệ thống phải biết **chẩn đoán, truy vết, liên kết các sự kiện
> độc lập** để xử lý vấn đề trong hệ thống khách hàng dựa trên **kiến thức, kinh nghiệm,
> tài liệu**. Không phải chỉ có alert, **mọi thông tin, log trong hệ thống khách đều phải
> được phân tích, liên kết và xử lý**."

Đây là thước đo để đánh giá kiến trúc, KHÔNG phải một feature request. Đối chiếu bằng số
đo thật trên cluster (2026-07-30 23:50):

| trụ | trạng thái | bằng chứng |
|---|---|---|
| **Liên kết sự kiện độc lập** | ❌ engine có, **nối vào hư không** | `OMNI_SIEM_CORRELATION_ENABLED=true` nhưng `omni-siem-raw`=**0 msg**, `omni-siem-incidents`=**0 msg**, `corr:*`=**0 key**. Chưa từng liên kết một sự kiện nào. Chỉ nhận `Incident` dạng SIEM; 38k+4k message của đường chính không bao giờ tới. |
| **Mọi log đều được phân tích** | ❌ 90% thông tin vào ngõ cụt | `omni-knowledge-evidence`=**38 468 msg** vs `omni-diagnostic-evidence`=**4 163**. Đường 38k theo thiết kế **không LLM, không RAG**. `LOG_SAMPLE` → `_handle_log_sample` ghi vào `omni:knowledge:logs:{agent}:rolling`, docstring ghi *"RAG context for future queries"* — **grep toàn `src/` không có người đọc nào**, chỉ test tham chiếu; cluster hiện **0 key**. Kho ghi-rồi-quên. |
| **Kiến thức / kinh nghiệm / tài liệu** | ❌ cả ba đều rỗng | *Tài liệu*: `document_store.py:43` chỉ lưu `summary[:2000]` + metadata (INV_DATA_RESIDENCY) ⇒ tài liệu khách **không thể** làm cơ sở tri thức. *Kinh nghiệm*: `pattern_key` sập về ≤4 rổ (9/9 `affected_workload="unknown"`). *Kiến thức*: RAG bị bỏ qua **387/388**. |
| **Truy vết** | ⚠️ có trace nhưng mù một nửa | `pipeline.status` chỉ có `["ok","skip"]` — không biểu diễn được thất bại. |

### KẾT LUẬN KIẾN TRÚC (bổ sung cho "vấn đề gốc")
Bỏ 4 lane sang 9 domain đổi **nhãn** của incident, không đổi **yêu cầu rằng mọi thứ phải
là incident**. Toàn bộ hạ tầng sau gateway (severity → triage → RAG → LLM → advisory →
CRAT → Telegram) đòi một đối tượng có `alert_rule`/`severity`/`domain`. Thông tin **không
phải sự cố** có đúng hai đích: một bộ đếm (`+1/100 mẫu`) và một list Redis không ai đọc.

⇒ Omni hiện là **hệ xử lý alert có thêm ống dẫn số**, chưa phải hệ hiểu hệ thống khách.
Ba trụ user nêu không phải "thiếu tính năng" — chúng **không có chỗ đứng trong luồng dữ
liệu hiện tại**. Cửa duy nhất để một thông tin được suy luận là tự nâng mình thành ANOMALY.

Việc cần, theo thứ tự phụ thuộc (chưa được duyệt):
1. Một **kho sự kiện đã chuẩn hoá** (entity + thời gian + nguồn) mà MỌI signal đều ghi vào
   — metric, log, discovery, change, alert — không phân biệt "có phải sự cố không".
2. **Correlation chạy trên kho đó**, không phải trên topic SIEM rỗng. Engine union-find +
   entity extract đã có sẵn (`src/services/siem_correlation/`), chỉ đang nối sai nguồn.
3. **Kho tri thức thật**: tài liệu khách phải dùng được (đụng INV_DATA_RESIDENCY — cần
   quyết định của user: embed tại chỗ khách, hay nới bất biến, hay chỉ index cục bộ).
4. Chỉ khi có 1-3 thì "kinh nghiệm" mới tích luỹ được, vì mới có corpus để học.


Trạng thái: CHƯA SỬA GÌ. Mọi mục dưới đây đã được kiểm bằng đọc code; các mục đánh
dấu ✅CHẠY là tôi tự chạy lại để xác nhận, không tin agent trên giấy.

## Chủ đề xuyên suốt
Ba trụ của tầm nhìn "nhân viên SRE" — tự phán bằng baseline, tự xin quyền, học từ kinh
nghiệm — đều KHÔNG đứng ở tầng cơ chế, chỉ đứng ở tầng tài liệu. Hình dạng chung:
**cơ chế an toàn tồn tại, có tài liệu, có test, và không gác gì cả.**

## CHẶN PHÁT HÀNH

### 1. RCE quyền root trên host khách ✅CHẠY
`src/pkg/diagnostics/validator.py:172-176`. `_AWK_WRITE`/`_AWK_PIPE`/`_AWK_GETLINE_READ`
đòi dấu nháy nằm SÁT toán tử. Awk cho phép gán vào biến rồi redirect qua biến.
PoC vượt chốt (chạy thật qua `validate_command`):
- `awk 'BEGIN{f="/tmp/pwn"; print "x" > f}'`        → ghi file tuỳ ý
- `awk 'BEGIN{c="id"; c | getline out; print out}'` → exec lệnh tuỳ ý
- `awk 'BEGIN{p="/etc/shadow"; getline l < p; print l}'` → đọc file tuỳ ý
Đối chứng: dạng literal / `system()` / `ENVIRON` đều CHẶN đúng.
Comment `:175-176` tự phản bác: "tên file dạng chuỗi HOẶC BIẾN" — biết mà regex không bắt.
Cộng: `User=` rỗng trong unit ⇒ ✅CHẠY `ps -o user=` trên cust-app trả **root**.
⇒ `is_path_readable`/`_looks_secret` chỉ chặn theo TÊN đường dẫn, chưa bao giờ là biên OS.
Kết luận phương pháp: awk là ngôn ngữ đầy đủ, regex trên chuỗi không cấm được nó ghi.
Phải tokenize, hoặc chặn thẳng `>` `|` `getline <` bất kể ngữ cảnh.

### 2. `scope_grant` là nghi thức, không phải cổng ✅CHẠY
`granted_scope` chỉ đọc ở `advocacy.py` (bên đề xuất), `console/app.py:790`,
`competency.py:101` (hiển thị). grep `kafka_actions_consumer.py`,
`evidence_mutate_emit.py`, `analyst_agentic_loop.py` → KHÔNG có `get_grant`.
Duyệt AUTO_EXECUTE cho một pattern không đổi hành vi gì.

### 3. Nhãn CRAT/SOX không đứng được
- HEAD chỉ ở Redis (`chain_writer.py:28-29`). Mất ⇒ `prev_hash=_GENESIS_HASH`, chain
  lặng lẽ mọc lại từ seq=1. `verify_chain` trả ok=True "chain_valid"; list rỗng trả
  ok=True "empty_chain"; `scripts/crat_integrity_check.py:30-33` return 0 khi rỗng.
- Topic `omni-audit-chain` compact + key=seq ⇒ block mới ĐÈ block lịch sử cùng seq.
- `verifier.py:100` `if sig_hex and pub_hex` ⇒ xoá chữ ký thì verify xanh.
- `omni-fullstack-rbac.yaml:302-303` SA có `secrets: get,list` cluster-wide ⇒ bên bị
  audit đọc được khoá ký. Không có non-repudiation.
- `tests/chaos/test_chaos_crat_corruption.py:80-94` `except AuditLedgerError: pass`
  chấp nhận cả hai kết cục ⇒ test xanh là tín hiệu giả.

### 4. Vòng học không thể học ✅CHẠY
`advisory_ack.py:152` `advisory_pattern_key({"lane": lane, "alertname": alertname})`:
- `lane` ← `resolve_proof_lane()` = **trục B** (`resource|state|app_log|siem`)
- `alertname` ← `advisory.affected_workload` = văn bản LLM sinh
⇒ trục B chảy vào khe của trục A, ngay trong phiên tôi viết cảnh báo ba trục.
Sau 0014, mọi proof_lane → `unknown` ⇒ `pattern_key_domain=sha256('unknown|<wl>')`.

## TỰ PHÁN BẤT THƯỜNG — nền móng toán học sai (agent chạy số chứng minh)
- `three_sigma.py:83-101` LPUSH rồi LRANGE ⇒ mẫu đang chấm nằm trong cửa sổ tính σ.
  Đại số: **z_max = √(n−1)**, độc lập giá trị. n≤10 ⇒ z không thể >3. REMOTE_WINDOW=60
  ⇒ trần 7.68. Sự cố giữ mức cao bị tự nhập baseline sau ~6 mẫu ≈ 6 phút.
- Host phẳng: σ=0.0298, CPU nhích lên 5.2% ⇒ z=5.06 ⇒ ANOMALY. `MIN_STDDEV=1e-9` chỉ
  chặn σ đúng bằng 0.
- `disk_percent` đơn điệu ⇒ z phẳng 1.703 khi đĩa bò 70→99%, KHÔNG cảnh báo nào.
  Ngược lại 40→41% cho z=7.7.
- `knowledge_pipeline.py:162` `use_static = level != AUTONOMOUS` ⇒ ở AUTONOMOUS,
  disk 99% + z=1.703 ⇒ **0 cảnh báo**. Càng "tin" host, Omni càng mù.
- Dedup `omni:knowledge:promoted:*` lưu hằng `"1"`, không so severity ⇒ sự cố 2 tiếng
  = 1 cảnh báo rồi im 114 phút.
- Ba lớp bảo vệ cùng mù một kịch bản (đĩa đầy ở AUTONOMOUS) vì lớp thứ ba cũng chết:
  `domain_signals.py:393-410` đọc `cpu_pct/mem_pct/disk_pct`, producer
  `system.py:45-49` phát `cpu_percent/mem_percent/disk_percent`. **Hai bộ từ vựng
  song song.** Cùng lớp bug với `ANOMALY` vs `FAILED` đã trả giá.

## THANG TỰ CHỦ MỞ BẰNG BÙA SỐ
- `ConfidenceLevel`: 2 call site `add_confidence` = `+1/100 mẫu` và **`+20/document`**.
  Không cái nào đối chiếu dự đoán với thực tế. AUTONOMOUS = "sống 5.2 ngày" hoặc mua
  bằng 4 file PDF. `decay_confidence` là **code chết** (call site duy nhất là test).
  CLAUDE.md:62 mô tả như đang chạy ⇒ drift tài liệu do tôi.
- Cổng nâng tier: `record_rejected`/`record_false_positive` KHÔNG có call site
  production. Shadow ⇒ ZSET rỗng ⇒ `wilson_lower_bound(accepted,accepted)→1.0`,
  `fp_rate≡0` ⇒ hai điều kiện chất lượng KHÔNG THỂ fail. Chốt duy nhất còn lại là
  `graduated_playbooks>=1` — fail-closed **tình cờ**.
- `advisory_ack.py:309` `if verdict != VERDICT_INCORRECT` ⇒ PARTIAL và `verdict=None`
  (nút "đã đọc") đều tính accepted ⇒ thổi acceptance-rate ⇒ mở cổng FULL_AUTO.
- Số đo độ chính xác thật DUY NHẤT (`scoring.build_competency_report`: Wilson +
  unjudged_ratio + recurrence_rate) không nối vào cổng nào.

## VI PHẠM BẤT BIẾN
- INV_DATA_RESIDENCY: `analyst_agentic_loop.py:815-824,1631-1663` stdout lệnh chẩn
  đoán nhét thẳng vào prompt LLM (`[:2800]`,`[:3500]`); "sanitize" duy nhất chỉ thay
  tên pod. Catalogue cố ý cho đọc log ứng dụng (email/token/SQL kèm giá trị).
- Kill-switch không gate ở producer: `emit_execute_mutate` — hàm DUY NHẤT ghi vào
  `omni-actions` — không kiểm `omni_auto_execute_enabled` lần nào. Switch=false vẫn
  sản xuất EXECUTE_MUTATE + ghi CRAT `MUTATION_ENQUEUED`. Không có guard tuổi message
  ⇒ mất offset + switch bật = thực thi lại 7 ngày mutate.

## FILTER DOMAIN Ở CHỖ MUTATE ĐÃ CHẾT
`autonomous_decider.py:412` đọc `ev.get("lane") or ev.get("stream_tag")` từ
`anomaly_event_min` — dict KHÔNG có hai khoá này (khoá thật ở nơi khác là `stream_tags`
số nhiều). ⇒ `ln=""` ⇒ `matcher.py:68-74` `if trig.lanes and ln and ...` chết ⇒
playbook khai `domains=["database"]` khớp MỌI domain. Test cô lập ở tầng matcher không
dựng `anomaly_event_min` thật nên không bắt được.

## THẺ TELEGRAM — mọi lỗ cơ chế đã hiện ra ở đầu ra người đọc ✅CHẠY
Lấy 9 advisory thật từ Redis cluster (`omni:crat:llm_reason:*:advisory`):
- `"trace_id": "<copy from input>"` — **9/9**. LLM nhại placeholder của prompt. Đúng
  lớp bug đã ghi ở `project_diag_grounding_gate_2026_07_13`, tái diễn ở trường khác.
- `affected_workload: "unknown"` — **9/9**. Khoá học KHÔNG phân mảnh như tôi báo cáo
  ban đầu mà **SẬP VỀ MỘT ĐIỂM**: `sha256(proof_lane|"unknown")` ⇒ toàn hệ tối đa 4
  pattern. Đĩa đầy cust-edge + hết RAM cust-db + K8s unavailable + app panic = CÙNG
  một pattern. `occurrence_no` đếm sự cố không liên quan; "lần thứ N" trên thẻ nói dối.
- `root_cause: "Disk usage at 92.8% exceeds threshold, causing OOM events."` +
  `confidence: "high"` — **bịa cơ chế nhân quả** (đĩa ≠ bộ nhớ), rồi sinh
  `verification_steps` (`iostat`) để chứng minh liên kết bịa. Cùng thẻ có
  `forecast.confidence: "low"` + "No temporal evidence" ⇒ **tự mâu thuẫn trong chính nó**.
- Hai thẻ "92.8% on cust-edge" và "92.8% on cust-db" — số giống hệt. Truy ra:
  `system.py:41` `psutil.disk_usage("/")`, ba VM OrbStack dùng chung `/dev/vdb1`.
  ✅CHẠY: cả 3 VM đều trả **13.1% (23G/182G)**. ⇒ `disk_percent` KHÔNG phải đĩa của
  host; ba baseline "độc lập" dựng trên cùng một chuỗi số; một điều kiện thật sinh ba
  sự cố/ba vòng ReAct/ba lần LLM. Trên máy khách thật, `/` bỏ qua đúng chỗ hay đầy
  (`/var`, `/data`). Cùng lớp với memory `/mnt/mac`: phạm vi mount chưa bao giờ giải
  quyết ở collector, chỉ vá ở tầng suy luận.
- 3/9 advisory nội dung là "Insufficient evidence for ..." nhưng vẫn đi hết đường ống,
  tốn LLM, mở ca, cộng `occurrence_no`, và nếu bấm "đã đọc" thì cộng `accepted`.
  ⇒ Omni tự thưởng điểm cho việc thú nhận không biết gì.

## CHỖ TÔI NGHI SAI — ghi lại cho công bằng
- `unknown` KHÔNG rơi lặng: Priority-1 trả "high" kể cả unknown; KPI cố ý giữ;
  `policy.py:63` chặn `unknown==unknown` khớp giả.
- Catalogue fail-closed THẬT: `get_catalog` cache cả lỗi rồi re-raise.
- Ngân sách bước ĐÃ CÓ: `for step in range(total)`, 5-8 lượt, timeout 30s/lệnh.
  Handoff tôi ghi "chưa làm" là SAI — chỉ thiếu deadline wall-clock tổng (MEDIUM).
- Nhãn phạt CÓ call site ở sổ ca (memory của tôi lỗi thời): `accepted=False` cho
  VERDICT_INCORRECT, FROZEN reachable, HITL REJECTED → INCORRECT. Vấn đề nằm ở tầng
  quyết định nâng tier, không phải tầng ghi nhận.
- 14/19 đường CRAT fail-closed đúng thứ tự audit→emit. Phần dòng chảy viết cẩn thận.
- 4 bug ghép-theo-vị-trí phiên trước đã sửa đúng, không có bản thứ ba.

## MEDIUM còn lại
- `sanitize.py:135-138` `str(ef)[:600]` cắt giữa cấu trúc dict trước khi vào prompt.
- `network.py:98-101` `_prev_listeners` không có giới hạn tuổi ⇒ mù nhiều chu kỳ rồi
  tỉnh lại có thể báo "mất cổng" giả.
- `hitl_link.py:41` `case_id=f"case:{trace_id}"` vs `advisory_ack.py:166`
  `case_id=trace_id` ⇒ verdict HITL rơi vào ca mồ côi, mở ca THỨ HAI với pattern_key
  dạng văn bản. Docstring `hitl_link.py:36-39` khẳng định ngược lại.
- `0014` có thể vỡ trên dữ liệu thật: hai old_key khác nhau map về cùng new_key,
  `NOT EXISTS` đánh giá trên snapshot trước câu lệnh ⇒ vi phạm PK. (chưa chạy được)
- `omni_worker.py:1312-1318` set `ctx.admin_pool` TRƯỚC `run_migrations`, except chỉ
  log ERROR ⇒ schema dở dang, mặt phẳng học tắt lặng lẽ sau một dòng log.
- `store_scope.py:172-199` `open_request`/`active_cooldown` chỉ tra `pattern_key=$2`,
  không tra `pattern_key_legacy` ⇒ cooldown 14 ngày có thể bị vượt sau khi khoá đổi.
- `pkg/autonomy/gate.py:106` `if redis is not None` ⇒ redis=None bỏ hẳn CRAT, vẫn trả
  resolved_level kể cả FULL_AUTO. Fail-OPEN.
- `kafka_actions_consumer.py:295-301` rollback cố ý không fail-closed (safety-over-audit)
  ⇒ câu "MUST succeed trước action dispatch" trong CLAUDE.md sai như tuyên bố tuyệt đối.
- Redis không auth (`redis://redis:6379/0`, không requirepass/ACL) + `resolve_tier` tin
  cache ngay ⇒ `SET omni:cfg:tier:default auto` từ bất kỳ pod nào = leo tier.
  `_apply_plan_ceiling` return sớm khi `repo is None` ⇒ không có trần.

## RAG / LLM / SYSTEM PROMPT — truy theo trace thật `ra-d1b29771ee4c` (2026-07-31)

Đẩy sự cố thật (stop nginx cust-edge), truy Loki+Tempo+Redis theo trace id.

### LLM engine + system prompt: KHÔNG ngu, nhưng hardcode + có 2 chỗ ngu thật
- `diagnosis_loop.py:45` `_DIAGNOSIS_SYSTEM_PROMPT` là hằng chuỗi hardcode NHƯNG nội dung
  tốt (EVIDENCE PRIORITY, tool-selection, INV_NO_DATA_EXFIL, blast-radius, JSON strict).
  Ca nginx: LLM chọn đúng `systemctl status`+`journalctl`, kết luận đúng root cause
  (upstream cust-app not found — đã grep journal xác minh), confidence 0.95.
- 🔴 **Ngu 1**: `diagnosis_loop.py:358` `_fallback_remediation_steps` — LLM để trống
  remediation ⇒ hàm **match keyword** chèn bước CỨNG. Ca CPU "operating normally" rơi
  nhánh mặc định ⇒ khuyên `df -h which partition is full`. Khuyến nghị rác trên thẻ KHÔNG
  do LLM, do keyword-matcher hardcode.
- 🔴 **Ngu 2**: prompt cào bằng — một prompt cho mọi domain, không ngữ cảnh
  network/database/security riêng.
- Tempo: 6 span nhưng **mọi span 0ms** — chỉ cột mốc, KHÔNG bọc lời gọi LLM ⇒ token/thời
  lượng LLM không trace được.

### 🔴🔴 Vì sao RAG NO HIT — khẩu súng còn khói (3 tầng, mỗi tầng đủ giết)
```
idx:action_experience                  = 431 docs
idx:action_experience:staging-sim      =   0 docs  ← tenant ca nginx tra vào ĐÂY
idx:action_experience:tenant-replay-01 =   0 docs
idx:itops_sop_ledger                   = 1093 docs ← KHO SOP, remote path KHÔNG tra
```
1. **Cô lập tenant giết recall.** `recall_playbook_advisory` (`archivist.py:130`) lọc theo
   `tenant_id`; cô lập bằng INDEX riêng (`scoped_collection_name`). Ca `staging-sim` tra
   `action_experience:staging-sim` = **RỖNG**. 431 kinh nghiệm nằm ở index `default`,
   không với tới. ⇒ **mọi tenant khách thật đều index rỗng ⇒ RAG không bao giờ hit.**
2. **Sai kho.** Triage chỉ tra `action_experience`. Kho **1093 SOP** (`itops_sop_ledger`)
   không được tra ở remote path (cả triage lẫn diagnosis loop).
3. **RAG không vào prompt.** `diagnosis_loop.py` prompt nhận `[VM PROFILE]`+`[INITIAL
   EVIDENCE]`, KHÔNG một dòng recall/SOP. Recall chỉ dùng để ĐỊNH TUYẾN (KNOWN vs UNKNOWN),
   chưa bao giờ là ngữ cảnh cho LLM. ⇒ LLM chẩn đoán lại từ 0 mỗi lần, kể cả nginx đã hỏng
   đúng kiểu này từ 30/06 (log còn ghi).
⇒ `MEMORY.md` ghi "RAG HLEN 1019" như tài sản — có 1093 điểm thật, **không đường remote
nào chạm tới**. Đúng "RAG HLEN là tín hiệu giả" (project_autonomous_sre_g1g4).

### Nhân bản đa tenant trên VM dùng chung (điểm mới từ 2 thẻ)
`3sigma:remote:staging-sim:cust-edge:cpu` + `3sigma:remote:tenant-replay-01:cust-edge:cpu`
— hai tenant chạy agent trên CÙNG cust-edge ⇒ một CPU 12.x% bình thường sinh **2 thẻ báo
động giả**, mỗi tenant một cái. Cùng cơ chế `disk_percent` dùng chung `/dev/vdb1`.

### Thẻ Telegram — lỗi trình bày mới
- `remote_diagnosis_emitter.py:47` `_short_trace=trace_id[-8:]`: `ra-xxx-cpu_percent` ⇒
  TRACE `#_percent`, MỌI ca CPU trùng nhau, không truy vết được.
- `os_hoót`: escape Markdown chèn `\` giữa `os_host` rồi bị cắt. Header CPU mất sạch dấu
  tiếng Việt ("mang: 1 cong lang nghe VUA DONG") — đường render khác tước dấu.

### Bằng chứng vận hành đối chứng (giá trị nhất phiên)
| | ca nginx (thật) | ca CPU 12.x% (giả) |
|---|---|---|
| phát hiện | ✅ 2 đường độc lập | ❌ σ nhỏ ⇒ z bung |
| chẩn đoán | ✅ đúng, verify được | ❌ "normal 100%" mâu thuẫn tiêu đề |
| khuyến nghị | ✅ đúng file/lệnh | ❌ keyword fallback hardcode sai chủ đề |
| truy vết | ✅ #9771ee4c | ❌ #_percent trùng |
| RAG | skip no_hit | skip no_hit |
| nhân bản | 1 | ×2 theo tenant |
⇒ động cơ chẩn đoán tốt, chôn dưới lớp phát-hiện tạo báo giả (nhân theo tenant) + lớp
trình-bày làm hỏng cả thẻ đúng lẫn sai. User nhận 3 thẻ, 1 có giá trị, và nó ít nổi nhất.

---

## KẾ HOẠCH THỰC THI ĐÊM 2026-07-31 (user duyệt "thực hiện toàn bộ", sáng kiểm trên UI)

Nguyên tắc: **ưu tiên thứ làm hệ TRUNG THỰC và QUAN SÁT ĐƯỢC trước**, vì không có thước đo
thì mọi bản vá khác không chứng minh được. Không đụng `INV_DATA_RESIDENCY` trong đêm nay
(chờ user quyết) — mọi việc dưới đây làm được mà KHÔNG cần nới bất biến đó.

**PHẠM VI ĐÊM (user chốt 2026-07-31): A + B + C + D + E + kill-switch producer.**
User trả lời AskUserQuestion chọn A+B+D+E, sau đó nhắn "Cả phần 1-4 nữa chứ" ⇒ thêm mục
2 (từ vựng metric), 3 (baseline: use_static/disk/sàn σ), 4 (kill-switch gate producer).
Không đụng INV_DATA_RESIDENCY. Commit từng lô + push. add đúng file source, KHÔNG add-A.

**Lô A — An toàn (✅ XONG, 88 test pass):**
- A1. ✅ Vá awk RCE `validator.py:169-217`: guard neo theo TỪ KHOÁ (print/printf+`>`|`|`,
  getline+`<`|`|`, system, ENVIRON) thay vì đòi dấu nháy sát toán tử. 3 PoC biến CHẶN,
  5 ca hợp lệ (pattern `$3>90`, regex, print trường) CHO QUA. Test khoá ở
  `test_diagnostic_catalog.py`.
- A2. **"root" (item 1) — CỐ Ý KHÔNG đổi VM đang chạy đêm nay.** Vá awk đã bịt vector RCE
  cấp tính. Hạ agent xuống non-root đòi đổi ownership `/opt/omni-remote-agent` + inbox +
  quyền systemctl/journalctl trên 3 VM — đổi mù giữa đêm có thể khoá agent ra ngoài. Ghi
  làm slice riêng có kiểm từng bước. Chặn khai thác quan trọng hơn (awk) đã xong.

**Lô C — Phát hiện đúng (thêm theo item 2-3):**
- C1. Sàn σ tương đối + sàn biên độ tuyệt đối (z chỉ tính khi lệch ≥ N điểm phần trăm).
- C2. `use_static` KHÔNG tắt ở AUTONOMOUS.
- C3. `disk_percent` tách khỏi z-score sang ngưỡng/tốc-độ-cạn.
- C4. Từ vựng metric `*_percent`/`*_pct` alias (item 2).

**Lô F — Kill-switch gate ở producer (item 4):**
- F1. `emit_execute_mutate` kiểm `omni_auto_execute_enabled` TRƯỚC khi ghi `omni-actions`.
- F2. Guard tuổi message ở `kafka_actions_consumer` (bỏ qua action quá cũ khi replay).

**Lô B — Trung thực đầu ra (làm hệ hết rác, để sáng nhìn thấy sạch):**
- B1. Cổng chống nhại: từ chối/không dùng advisory có `trace_id` chứa `<...>` hoặc không
  khớp trace thật.
- B2. `_short_trace` dùng phần hash ĐẦU của trace, bỏ đuôi metric ⇒ TRACE duy nhất/ca.
- B3. Sửa mất-dấu tiếng Việt ở header remote_diagnosis_emitter (escape đúng, không cắt giữa).
- B4. `verdict=INVESTIGATE` + "operating normally"/insufficient ⇒ KHÔNG phát thẻ báo động,
  KHÔNG mở ca, KHÔNG fallback remediation. Chỉ ghi observed.

**Lô C — Phát hiện đúng (giảm báo động giả):**
- C1. Sàn σ tương đối + sàn biên độ tuyệt đối (z chỉ tính khi lệch ≥ N điểm phần trăm).
- C2. `use_static` KHÔNG tắt ở AUTONOMOUS (giữ cận trên tĩnh mọi bậc).
- C3. `disk_percent` tách khỏi z-score (đơn điệu) ⇒ chuyển sang ngưỡng/tốc-độ-cạn.
- C4. Từ vựng metric: `domain_signals.py` đọc cả `*_percent` lẫn `*_pct` (alias) ⇒ lưới an
  toàn severity sống lại.

**Lô D — RAG thật sự hoạt động (đúng trụ user):**
- D1. Recall: fallback về index `default` khi index tenant rỗng (cross-read an toàn, chỉ
  đọc SOP/experience của chính lab — không phải dữ liệu khách).
- D2. Thêm `itops_sop_ledger` (1093 SOP) vào đường tra của remote triage.
- D3. **Nhét recall/SOP vào prompt diagnosis loop** (mục `[KIẾN THỨC LIÊN QUAN]`) ⇒ LLM
  chẩn đoán CÓ kiến thức, không từ số 0.

**Lô E — UI cho user kiểm (yêu cầu "sáng dậy phải nhìn thấy, test lại được"):**
- E1. Trang provider `/diagnostics` (hoặc mở rộng trang hiện có): liệt kê ca gần đây, mỗi ca
  bấm vào xem TỪNG BƯỚC (EVIDENCE→RAG→LLM turn 1..n→CRAT→DISPATCH) với chi tiết thật lấy từ
  `omni:diag:session:*` + `omni:trace:stages:*`.
- E2. Nút "Test lại": inject một sự cố mẫu (service down / disk / cpu) qua gateway, rồi
  poll hiển thị kết quả — để user tự đẩy và xem.
- E3. Hiện rõ: RAG hit/miss + điểm, prompt đã gửi (rút gọn), quyết định, CRAT seq.

Mỗi lô có test riêng, chạy full suite cuối. KHÔNG commit/push tự động (chờ user) — trừ khi
user chỉ thị khác ở phần confirm.

## THỨ TỰ VÁ ĐỀ XUẤT (chưa được duyệt — danh sách đầy đủ, một số nằm ngoài đêm nay)
A. Cổng chống nhại: từ chối advisory có trace_id không khớp / chứa `<...>`
B. `affected_workload=="unknown"` không được làm khoá học
C. Cổng nhân quả: root_cause nối hai domain khác nhau ⇒ hạ confidence hoặc chặn
D. `disk_percent` theo mount thật của khách, không phải `/`
E. `verdict=INVESTIGATE` + insufficient evidence ⇒ không mở ca, không tính accepted
1. awk + root
2. từ vựng metric `_pct`/`_percent`
3. `use_static` đừng tắt ở AUTONOMOUS + tách disk khỏi z-score + sàn σ/biên độ
4. kill-switch gate ở producer + guard tuổi action
5. pattern_key từ domain collector tự khai + alertname chuẩn hoá, host thành cột riêng
6. nối scoring vào cổng tier, cắt PARTIAL/None khỏi accepted, thêm nguồn FP thật
7. scope_grant thành cổng thật
8. CRAT: neo độ dài chain, verify bắt buộc chữ ký, gỡ secrets:get,list khỏi SA
9. hạ nhãn SOX/PCI trong CLAUDE.md, sửa dòng ngân sách bước + decay_confidence
