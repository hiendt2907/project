# Plan: Đóng gap Production còn lại — Autonomous SRE Vision (2026-07-23, vòng 2)

> Direct-edit mode (không branch/PR — trunk-based, xem CLAUDE.md AUTONOMY RULES).
> Baseline: HEAD `63b20c5` (đóng 4 gap kiến trúc AOIP — vòng 1, đã commit).
> Nguồn gap: audit 18-domain `docs/architecture/ASSESSMENT_autonomous_sre_v2.md` (2026-07-22,
> tổng thể ~71%) + xác nhận lại bằng đọc source thật (Explore agent, 2026-07-23 — xem bằng
> chứng inline mỗi phase, không suy diễn từ audit cũ).

## Mục tiêu

Vòng 1 đóng gap **kiến trúc** (AOIP capability/confidence/knowledge/question). Vòng 2 này
đóng gap **vận hành/governance** — các nhánh mà kiến trúc đã đúng nhưng runtime chưa chứng
minh sống, hoặc bypass gate đã thiết kế. Tiêu chí nghiệm thu: mỗi phase phải có bằng chứng
runtime thật (không phải chỉ code tồn tại), theo đúng thói quen đã dùng ở vòng 1.

## Chỉ thị ràng buộc của user (bắt buộc, đọc trước khi thực thi bất kỳ phase nào)

- **Đang ở giai đoạn LAB** (OrbStack, namespace `multi-agent`), CHƯA phải production thật.
- Với RBAC hoặc mutate/execution gate: **KHÔNG thắt chặt trong vòng này** — nới lỏng tối đa
  trong phạm vi lab để test được luồng thật (HITL escalation, SIEM chain, mutate qua
  executor). Phase 1-4 được phép mutate/test thật, **không cần hỏi lại xin phép RBAC**.
- Việc siết RBAC + siết lại mutate gate đã nới lỏng **chỉ được viết thành plan** ở Phase 5,
  **KHÔNG thực thi tự động** trong vòng lab này — gắn nhãn rõ "production cutover only".
- Không mở lại `FRAMEWORK_LAWS.md` (constitutional-frozen).
- Không tự ý commit/push git — hỏi trước mỗi lần (áp dụng như vòng 1).

## Thứ tự phase: 1 → 2 → 3 → 4 → 5 (5 chỉ viết plan, không chạy)

Lý do thứ tự: HITL (1) là điều kiện tiên quyết để test an toàn Phase 3 (SIEM cần escalate
lên người khi nối gate) — làm trước. omni-core (2) độc lập, xen giữa để không block HITL.
SIEM gate (3) phụ thuộc HITL đã sống. Portal (4) độc lập hoàn toàn, có thể chạy song song
thực tế nhưng xếp cuối theo độ ưu tiên thấp hơn (không chặn autonomy). Phase 5 luôn cuối,
không chạy.

## Quy trình mỗi phase thực thi (1-4)

`/plan` → tdd (test trước, `FakeRedis(decode_responses=True)` cho ZSET, `asyncio_mode=auto`)
→ code → `/code-review` → `/verify` (bằng chứng runtime thật, không chỉ pytest xanh) →
cập nhật `docs/handoffs/CURRENT_SESSION.md` (≤20 dòng).

---

## Phase 1 — Xác định + khôi phục đường escalate-cho-người thật (KHÔNG giả định "chỉ cần scale dispatcher")

**Model**: Opus (điều tra sâu — có khả năng phải đảo ngược 1 quyết định kiến trúc, cần
thận trọng).

**SỬA SAU ADVERSARIAL REVIEW (CRITICAL-1) — đọc trước khi làm bất kỳ việc gì**: giả định
ban đầu "dispatcher scale-0 = drift cần khôi phục" là SAI. Review (Opus) đã đọc code thật
và xác nhận: `src/workers/advisory_hitl_compat.py:11-22` — `AdvisoryHITLCompat`,
`OMNI_HITL_ENABLED = False`, docstring ghi rõ *"deprecate omni-hitl-pending; route to
Telegram"*, *"Now: Alert → Advisory Analyst → Telegram (not via omni-actions)"*. Producer
duy nhất vào topic là `emit_hitl_pending` (`src/workers/evidence_mutate_emit.py:360-380`,
gọi từ `evidence_consumer.py:2110,3049`) — hàm này **return im lặng** trừ khi
`omni_hitl_routing_enabled=True` (mặc định **False**, `advisory_hitl_compat.py:45-47`).
Nghĩa là: trong lab hiện tại, KHÔNG message nào được phép vào `omni-hitl-pending` **theo
đúng thiết kế hiện hành** (Advisory Mode qua Telegram) — dispatcher scale-0 là hệ quả hợp
lý, không phải bug.

**Việc cần làm**:
1. Đọc đầy đủ `advisory_hitl_compat.py` + `evidence_mutate_emit.py:344-431` — xác nhận lại
   claim trên, và đọc nơi Advisory Mode thật sự gửi Telegram hiện nay (analyst pipeline /
   `hitl_telegram.py` hay tương đương) để xác nhận đường Telegram-hiện-hành có thật sự
   chạy sống hay cũng chỉ là code-tồn-tại-chưa-chứng-minh.
2. **Quyết định rõ ràng, KHÔNG tự ý chọn** — trình bày 2 lựa chọn cho user trước khi code:
   - **(a) Giữ Advisory Mode** (Telegram là kênh escalate chính thức, `omni-hitl-pending`/
     dispatcher-Kafka tiếp tục deprecated) → Phase 1 đổi mục tiêu thành: chứng minh đường
     Telegram-advisory chạy sống thật (không phải dispatcher), viết/chạy 1 test tích hợp
     thật cho đường đó.
   - **(b) Hồi sinh HITL-via-Kafka có chủ đích** (bật `omni_hitl_routing_enabled=true`) →
     đây là ĐẢO NGƯỢC quyết định kiến trúc đã ghi trong `advisory_hitl_compat.py` — PHẢI
     có xác nhận trực tiếp của user trước khi làm, không suy luận ngầm từ "code đã có sẵn
     nên dùng lại".
   Mặc định đề xuất: (a), vì đó là trạng thái thiết kế hiện hành; chỉ chuyển sang (b) nếu
   user xác nhận rõ.
3. Theo lựa chọn đã chốt, viết 1 test tích hợp thật (`tests/integration/`, dùng Kafka/Redis
   lab thật — không mock) mô phỏng: 1 advisory giả (severity đủ cao) → escalate qua đúng
   kênh đã chốt (Telegram hoặc `omni-hitl-pending`) → người vận hành approve thật → xác
   nhận action route đúng tới `omni-actions` (lưu ý: với `OMNI_AUTO_EXECUTE_ENABLED=false`,
   executor vẫn skip mutation thật ở tầng đó — "tới `omni-actions`" nghĩa là routing đúng,
   KHÔNG phải "mutation được thực thi").
4. Chạy test đó thật trên lab, chụp lại bằng chứng (log/offset/Redis key/Telegram message
   thật) — tiêu chí nghiệm thu là round-trip approve→routing thật, không phải chỉ "message
   vào topic".
5. Nếu chọn (a): giữ nguyên `omni-hitl-dispatcher` ở scale 0 + annotation
   `scaled-down-intentional` (đã đúng). Nếu chọn (b) và xác nhận chạy tốt: scale lên 1,
   viết rõ lý do đảo ngược quyết định vào `docs/architecture/` (không chỉ handoff).

**Không được làm**: không tự ý bật `omni_hitl_routing_enabled=true` mà không hỏi user
trước (đây là đảo ngược kiến trúc, khác với "nới lỏng RBAC/mutate để test" mà user đã cho
phép — 2 việc khác nhau); không xoá test unit hiện có.

**Exit criteria**: đường escalate-cho-người ĐÃ CHỐT (Telegram hoặc Kafka-HITL) chạy thật
end-to-end trên lab, có bằng chứng log/message thật. `docs/handoffs/CURRENT_SESSION.md`
ghi rõ lựa chọn (a)/(b) và lý do.

**PRECONDITION CHẶN PHASE 3 (HIGH-2)**: Phase 3 KHÔNG được bắt đầu cho tới khi Phase 1
chứng minh được 1 round-trip approve→routing THẬT (không chỉ "message được produce vào
topic mà không ai xử lý"). Nếu Phase 1 kết thúc mà đường escalate vẫn chưa sống thật,
Phase 3 phải dừng và báo cáo lại, không tự ý coi "produce thành công" là đủ điều kiện.

---

## Phase 2 — Fix 3 bug `omni-core`

**Model**: Sonnet.

**Bằng chứng đã xác nhận**:
- **Forecast Prometheus format** (SỬA SAU REVIEW — HIGH-1: đây là ASSUMPTION chưa verify
  bằng request thật, KHÔNG phải bug đã xác nhận): `src/workers/forecast_autonomous_loop.py:44-51`
  gọi `_duration_to_vm_window(...)` rồi truyền `start="now-1h"`, `end="now"` (literal) vào
  `fetch_range_dataframe`. `src/workers/sdk_service_tools.py:865-873`
  (`_duration_to_vm_window`) trả về `f"now-{d}"`. `src/metrics/prometheus_dataframe.py:33-42`
  (`fetch_range_dataframe`) gọi thẳng `_prometheus_get_json(..., "/api/v1/query_range",
  {"start": start, "end": end, ...})` — không convert epoch trong hàm này. NHƯNG: cùng
  pattern `now-Xh` cũng được dùng ở `query_prometheus_metrics`
  (`sdk_service_tools.py:662,718,921` — tool vẽ chart CPU/RAM cốt lõi) và
  `src/anomaly/sigma_calibrator.py:59-60` (3σ calibrator). Nếu các tính năng này đang chạy
  được trong lab thì `now-1h` ĐANG ĐƯỢC CHẤP NHẬN (endpoint tương thích) và KHÔNG có bug —
  còn nếu chúng cũng lỗi thì đây là sự cố lớn hơn phạm vi forecast. `autonomous_forecast_enabled`
  default=False (`settings.py:930-931`) → forecast loop tắt trong lab → 400 (nếu có) CHƯA
  TỪNG được quan sát thật. Bước 1 dưới đây BẮT BUỘC phải verify trước khi coi đây là bug.
- **deep_scout không escalate khi lỗi** (chỉnh mô tả — MEDIUM-3: không hoàn toàn "im lặng",
  có ghi `summary.errors`, chỉ thiếu retry+escalate): `src/init/deep_scout.py:355-361` —
  `except Exception as e: summary.errors.append(...); logger.warning(...)` — không raise,
  không retry khi Redis timeout; loop tiếp tục như thành công dù lỗi đã được ghi nhận.
- **Proactive dormant** (KHÔNG phải lỗi scheduling — giới hạn thiết kế thật): loop tick đều
  (`proactive_observer.py:994-1033`, mỗi `proactive_eval_interval_sec`=120s) — sống bình
  thường. Nguyên nhân dormant: `evaluate_proactive_triggers` (410-453) chỉ theo dõi **1
  PromQL rule hardcode duy nhất** (`settings.py:1263-1264`:
  `sum(kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"})`, threshold 0.0).
  Lab không có CrashLoopBackOff kéo dài → luôn 0 → không bao giờ trigger.

**Việc cần làm**:
1. **BẮT BUỘC TRƯỚC TIÊN**: `curl` (hoặc gọi hàm thật) endpoint Prometheus lab
   (`OMNI_PROMETHEUS_URL`, `/api/v1/query_range`) với `start=now-1h&end=now` y hệt code hiện
   tại đang gửi — xác nhận nó thật sự trả lỗi (400 hoặc tương đương). Nếu Prometheus lab
   CHẤP NHẬN format này (proxy tương thích/VictoriaMetrics-compat còn sót), đây KHÔNG phải
   bug — bỏ qua bước fix, ghi rõ lý do trong handoff, chuyển sang bug tiếp theo.
2. Nếu bước 1 xác nhận lỗi thật: fix TẬP TRUNG ở `_duration_to_vm_window`
   (`sdk_service_tools.py:865-873`) và/hoặc `fetch_range_dataframe`
   (`prometheus_dataframe.py:33-42`) để convert epoch — vì có ≥5 call-site dùng chung pattern
   (`forecast_autonomous_loop.py`, `query_prometheus_metrics` x3 dòng 662/718/921,
   `sigma_calibrator.py:59-60`), fix phải đóng TẤT CẢ, không chỉ forecast loop. Cần bật tạm
   `autonomous_forecast_enabled=true` trong lab để lấy evidence request thật (nhớ trả lại
   default sau khi xong, ghi vào handoff).
3. Test: request `query_range` thật với `start`/`end` epoch hợp lệ, xác nhận Prometheus lab
   thật trả 200 (không phải mock response) — cần bằng chứng HTTP request thật thành công,
   không chỉ unit test hàm convert. Exit criteria phải cover cả 3 nhóm call-site (forecast,
   query_prometheus_metrics, sigma_calibrator), không chỉ forecast loop.
3. Fix deep_scout: Redis timeout cụ thể (không phải `Exception` chung) phải retry có giới
   hạn (dùng pattern retry đã có trong repo, ví dụ nơi `hitl_dispatcher.py` retry+backoff)
   rồi mới fallback log — không được nuốt lỗi im lặng nếu retry hết vẫn fail; phải escalate
   (tăng error count có giám sát, không chỉ log).
4. Proactive rule: mở rộng danh sách PromQL rule có thể theo dõi (không hardcode 1 rule) —
   đọc `settings.py` xem có config đã hỗ trợ list rule chưa hay cần thêm field mới
   (`proactive_promql_rules: list[str]`, giữ field cũ tương thích ngược nếu vẫn dùng ở nơi
   khác — grep `proactive_promql` toàn repo trước khi đổi type).
5. Với mỗi fix, chạy thật 1 lần trên lab (Prometheus/Redis thật) để lấy bằng chứng, không
   chỉ dựa vào pytest.

**Không được làm**: không đổi `proactive_eval_interval_sec` mặc định; không thêm rule mới
mang tính suy đoán ngoài phạm vi "cho phép nhiều rule thay vì 1" — không tự chọn rule cụ
thể nào khác để thêm vào, chỉ mở rộng cơ chế.

**Exit criteria**: request Prometheus thật thành công (200, dữ liệu hợp lệ) từ forecast
loop; deep_scout không còn nuốt lỗi Redis timeout im lặng (có bằng chứng retry+escalate);
proactive loop có thể theo dõi >1 rule (chưa cần thêm rule cụ thể, chỉ cần cơ chế).

---

## Phase 3 — Nối SIEM correlation vào escalation tier gate

**Model**: Opus (thay đổi luồng quyết định autonomy — rủi ro cao nếu sai).

**PRECONDITION (nhắc lại từ Phase 1, HIGH-2)**: KHÔNG bắt đầu phase này cho tới khi Phase 1
đã chứng minh round-trip approve→routing thật trên đường escalate đã chốt (Telegram hoặc
Kafka-HITL). Nếu Phase 1 chọn (a) giữ Advisory Mode/Telegram, thì "route L3_HITL" ở phase
này nghĩa là route vào **đúng đường Telegram-advisory đó**, KHÔNG phải `omni-hitl-pending`
— sửa lại bước 2 dưới đây cho khớp lựa chọn thật của Phase 1, đừng giữ giả định
`omni-hitl-pending` một cách máy móc.

**Bằng chứng đã xác nhận**:
- `src/workers/siem_correlation_loop.py` (111 dòng, toàn bộ file): decode → produce
  `omni-siem-incidents` → `correlator.process()` → produce `omni-siem-chains`. Không dòng
  nào gọi `resolve_tier`/`tier_gate`.
- `grep -rn "resolve_tier|tier_gate" src/services/siem_correlation/*.py` → 0 kết quả.
- `src/services/analyst/chain_consumer.py:318-335` (`_emit`) emit thẳng
  `{"action": "suggest_remediation", ...}` vào `omni-actions` — không qua tier gate.
- So sánh: `src/workers/kafka_actions_consumer.py:528-545` (`_handle_execute_mutate`) CÓ
  gọi `resolve_tier` (từ `workers.tier_gate`) cho nhánh mutation — nhưng nhánh
  `suggest_remediation` (dòng 205-210) chỉ `mark_stage(..., "skip", detail="suggest-only
  (no execute)")`, không qua L1_AUTO/L2_SUGGEST/L3_HITL nào bất kể severity/confidence.

**Việc cần làm**:
1. Đọc `workers/tier_gate.py` (`resolve_tier` + cách `kafka_actions_consumer.py` dùng nó
   cho nhánh mutation) — đây là pattern chuẩn cần tái dùng, KHÔNG viết gate mới song song.
2. Sửa `chain_consumer.py._emit` (hoặc điểm tương đương) để trước khi produce
   `suggest_remediation`, gọi `resolve_tier(tenant, ...)` giống hệt nhánh mutation; theo
   kết quả tier: L3_HITL → route qua đường escalate đã chốt ở Phase 1 (Telegram hoặc
   `omni-hitl-pending`, KHÔNG mặc định là Kafka-HITL nếu Phase 1 chọn giữ Advisory Mode);
   L1_AUTO/L2_SUGGEST → giữ nguyên hành vi hiện tại (produce `omni-actions` như cũ).
   **(MEDIUM-1) Định nghĩa rõ envelope escalate**: `_emit` hiện produce
   `suggest_remediation` chỉ có `narrative/advisory`, KHÔNG có `tool_name`/`args` —
   trong khi `hitl_dispatcher._parse_pending` (222-235) và forward-sau-approve kỳ vọng 1
   tool cụ thể. Nếu route thẳng advisory-không-tool vào HITL, sau approve executor sẽ rơi
   vào nhánh skip vì thiếu tool → gate chỉ mang tính trang trí. Phải quyết định: hoặc SIEM
   escalation là "advisory-only" (chỉ cần Telegram thông báo cho người xem, không cần
   approve/action) — auto chọn nếu Phase 1 là (a); hoặc phải bổ sung tool đề xuất cụ thể
   vào envelope trước khi route HITL — chỉ cần nếu Phase 1 là (b).
3. Test: 1 chain giả với severity/confidence cao đủ để rơi vào L3_HITL theo tier hiện tại
   của tenant test → xác nhận nó đi đúng đường escalate đã chốt (không phải thẳng
   `omni-actions`).
4. Chạy thật trên lab: bơm 1 SIEM incident giả qua `omni-siem-incidents` → xác nhận chain
   ra đúng qua tier gate (log/Redis key/Telegram message thật), không chỉ pytest.

**Không được làm**: không đổi ngưỡng tier hiện có (`resolve_tier` logic) — chỉ NỐI SIEM
vào gate đã có, không sửa gate; không tự ý set `OMNI_AUTO_EXECUTE_ENABLED=true` để test;
không tự ý bật `omni_hitl_routing_enabled=true` ở phase này nếu Phase 1 đã chốt giữ
Advisory Mode — 2 quyết định phải nhất quán với nhau.

**Exit criteria**: 1 SIEM chain severity cao chạy thật trên lab, đi đúng qua `resolve_tier`
và route L3_HITL vào `omni-hitl-pending` (không bypass thẳng `omni-actions` nữa).

---

## Phase 4 — Portal: nâng tenant-portal lên gần parity provider-portal (audit + advisory detail)

**Model**: Sonnet.

**Bằng chứng đã xác nhận**:
- `ui/apps/provider-portal/app/`: có `audit/page.tsx` (86 dòng), `pipeline/page.tsx` (139
  dòng) + `pipeline/[traceId]/page.tsx` (116 dòng) + `TraceDiagnosisSection.tsx` (212 dòng),
  `incidents/page.tsx` (115 dòng).
- `ui/apps/tenant-portal/app/`: chỉ `missions/`, `understanding/`, `agents/`, `approvals/`,
  `incidents/` — `incidents/page.tsx` chỉ **14 dòng**. KHÔNG có `audit/` nào, KHÔNG có
  `pipeline/[traceId]` (advisory trace detail) nào.

**Việc cần làm**:
1. Đọc kỹ `provider-portal/app/audit/page.tsx` và `pipeline/[traceId]/` — xác định phần
   nào hiển thị dữ liệu **của riêng tenant đó** (an toàn để lộ cho khách hàng) vs phần nào
   chỉ dành nội bộ provider (ví dụ chi tiết multi-tenant, cấu hình vận hành) — đây là bước
   bắt buộc trước khi port, KHÔNG copy nguyên trang.
2. Port route `audit` cho tenant-portal — chỉ hiển thị audit chain của tenant hiện tại
   (theo tenant_id từ session/JWT, không phải toàn bộ), tái dùng API BFF đã có (kiểm tra
   `aoip-tenant-portal` backend đã có endpoint audit theo tenant chưa, nếu chưa cần thêm
   endpoint scoped-by-tenant, KHÔNG dùng thẳng endpoint provider).
3. Port route `pipeline/[traceId]` (hoặc tương đương `incidents/[id]`) cho tenant — advisory
   detail theo trace, scoped đúng tenant.
4. E2E test (Playwright, theo `tests/e2e_portals` pattern đã có — xem
   `project_productization_iteration24_portal_e2e_gate` trong memory) cho 2 route mới.

**Không được làm**: không lộ dữ liệu cross-tenant (kiểm tra kỹ mọi query có `WHERE
tenant_id = ...` thật, không tin field ẩn trên client); không copy toàn bộ trang provider
nếu có phần chỉ dành nội bộ (ví dụ danh sách tất cả tenant khác).

**Exit criteria**: tenant-portal có route `audit` + advisory-trace-detail thật, scoped
đúng tenant, E2E xanh trên `make e2e-portal`.

---

## Phase 5 — KHÔNG THỰC THI. Chỉ viết plan chi tiết: siết RBAC + siết mutate gate trước cutover production thật

**Model**: không áp dụng (không chạy phase này trong vòng lab hiện tại).

**Ràng buộc tuyệt đối**: Phase này CHỈ được thực thi khi user xác nhận trực tiếp đang
chuẩn bị cutover sang production thật (không phải lab OrbStack nữa). Không tự động chạy,
không tự ý bắt đầu dù 4 phase trên đã xong.

**Bằng chứng đã xác nhận (Explore agent 2026-07-23 + adversarial review Opus — HIGH-3 sửa
lại phần đếm thiếu và bổ sung item quan trọng hơn)**:
- **KHÔNG tìm thấy file RBAC nào trong git hiện tại** định nghĩa `ClusterRoleBinding` +
  `cluster-admin` cho `omni-worker`/`omni-analyst`/`omni-prober` — các file này đã bị
  `git rm` ở commit `915e509` (split-role consolidation), và bản thân nội dung cũ (đọc qua
  `git show 915e509^:...`) tự ghi rõ "cluster-admin binding removed", dùng ClusterRole hẹp.
- Bằng chứng gián tiếp (suy đoán, cần xác nhận cluster thật): `prober-rbac.yaml` bản cũ có
  `resourceNames: ["omni-worker-cluster-admin"]` — tức có 1 object tên này từng/đang tồn
  tại **ngoài phạm vi IaC của repo**.
- **SA `omni-worker` được dùng bởi ≥3 workload** (đếm lại đúng, plan bản đầu chỉ liệt kê 2):
  `k8s/jobs/crat-integrity-check-cronjob.yaml:23`,
  `k8s/deployments/knowledge-ingest-cronjob.yaml:20`,
  **`k8s/deployments/sop-ingest-job.yaml:14`** — KHÔNG có file RBAC nào trong repo định
  nghĩa quyền cho SA này nữa (các Role tên `omni-worker-*` còn lại trong git thực ra bind
  cho SA `omni-fullstack`, không phải `omni-worker`).
- **Item quan trọng hơn, EDITABLE TRONG GIT NGAY** (bị bỏ sót ở bản đầu):
  `k8s/deployments/omni-fullstack-rbac.yaml:69-88`
  (`omni-fullstack-executor-mutate-lab` ClusterRoleBinding) +
  `omni-fullstack-rbac.yaml:281-310` (`omni-executor-mutate-lab` ClusterRole) cấp cho
  **pod đang chạy thật** `omni-fullstack` quyền cluster-wide `deployments` patch/update
  **VÀ `secrets` patch/update mọi namespace**, đã tự annotate "Lab only. Delete before
  promoting to prod." — đây là item cutover chính vì sửa được ngay trong repo, không phải
  suy đoán ngoài git như binding `omni-worker-cluster-admin`.
- **Kết luận**: gap `omni-worker-cluster-admin` là drift giữa **live cluster state và IaC**
  (ngoài phạm vi git, cần audit cluster thật để xác nhận), còn gap
  `omni-fullstack-executor-mutate-lab` là **over-privilege thật đang nằm trong git**, sửa
  được bằng cách xoá 2 khối yaml — 2 loại việc khác nhau, Phase 5 phải xử lý cả hai.

**Nội dung plan cần viết (khi tới lúc thực thi thật)**:
1. Audit trực tiếp cluster: liệt kê toàn bộ `ClusterRoleBinding`/`RoleBinding` thật đang
   gán cho `omni-worker`, `omni-analyst`, `omni-prober` — so với những gì git quản lý.
2. **Ưu tiên trước** (editable ngay): xoá `omni-fullstack-executor-mutate-lab`
   ClusterRoleBinding + thu hẹp `omni-executor-mutate-lab` ClusterRole (bỏ `secrets`
   patch/update cluster-wide, chỉ giữ đúng namespace + resource cần cho tool
   `k8s_patch_secret` đã gate bằng `required_evidence`/`MUTATE_TOOL_ALLOWLIST` theo
   CLAUDE.md) trong `omni-fullstack-rbac.yaml`.
3. Thiết kế RBAC tối thiểu mới cho `omni-worker` (đủ quyền cho cả 3 workload: crat-integrity
   -check, knowledge-ingest, sop-ingest — đọc/ghi Kafka, đọc PG audit tables, KHÔNG
   cluster-admin), viết vào git.
4. Xoá `ClusterRoleBinding` cluster-admin thật trên cluster (chỉ sau khi RBAC mới ở bước 3
   đã áp dụng và cả 3 workload đã chạy thử thành công với quyền hẹp).
5. Rà soát lại mọi thứ đã NỚI LỎNG trong Phase 1-4 vòng lab này, gồm **(MEDIUM-2, bắt buộc
   kiểm tra)**:
   - `OMNI_AUTO_EXECUTE_ENABLED` phải về lại `false` (nếu vòng lab có bật thử ở đâu đó).
   - `omni_hitl_routing_enabled` phải revert về `false` nếu Phase 1 từng bật để test lựa
     chọn (b) — trừ khi user xác nhận (b) là quyết định lâu dài.
   - `autonomous_forecast_enabled` trả về default nếu Phase 2 bật tạm để lấy evidence.
   - annotation `scaled-down-intentional` trên `omni-hitl-dispatcher` — bật lại nếu dispatcher
     thật sự chưa sẵn sàng nhận traffic thật ngoài lab.
   - xoá mọi tenant/unit test-only tạo trên lab (Phase 3, Phase 4).
6. Rollback plan nếu siết RBAC làm workload thật gãy (RBAC mới thiếu quyền nào đó phát hiện
   sau khi siết) — có bước revert nhanh về least-privilege cũ đã biết hoạt động, không
   revert thẳng về cluster-admin hay mở lại `secrets` cluster-wide.

**Exit criteria (khi thực thi thật, không phải bây giờ)**: 0 ClusterRoleBinding
cluster-admin nào còn active cho 3 SA legacy; `omni-fullstack-executor-mutate-lab` đã xoá/
thu hẹp; cả 3 workload (crat-integrity-check, knowledge-ingest, sop-ingest) vẫn chạy đúng
với RBAC hẹp mới; mọi biến môi trường/annotation đã nới lỏng cho lab (liệt kê ở bước 5)
được khôi phục hoặc ghi nhận rõ ràng lý do giữ nguyên là production-ready.
