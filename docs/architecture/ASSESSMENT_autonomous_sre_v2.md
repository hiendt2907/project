# MA TRẬN NĂNG LỰC ĐẦY ĐỦ — 18 Domain Audit (2026-07-22, runtime-verified)

> Audit toàn diện không giới hạn theo vài trục quen thuộc — 18 Explore subagent chạy song song,
> mỗi domain invoke đúng skill `omni-*` tương ứng trước khi khảo sát (bắt buộc theo CLAUDE.md),
> yêu cầu bằng chứng runtime thật (kubectl/kafka/redis/pytest), không suy diễn % từ code/test tồn
> tại. Tiêu chuẩn bằng chứng kế thừa từ
> [AUDIT_autonomous_sre_team_2026_07_22.md](AUDIT_autonomous_sre_team_2026_07_22.md) (bài học: cùng
> một "chưa chạy runtime thật" không được chấm lệch nhau 10-90% tùy hàng). Bổ sung/đối chiếu với
> [PRODUCT_PROOF.md](../product/PRODUCT_PROOF.md) (capability matrix operator-visible) và
> [ADR-001-canonical-agent-runtime.md](ADR-001-canonical-agent-runtime.md) (tiến độ migrate agent).

## Kết quả tổng thể: **~71%**

(Đối chiếu tự nhiên với nhận định gốc §0 bên dưới — "~70% nền tảng có thể giữ nguyên" — viết từ
đọc kiến trúc tĩnh 2026-06-29; con số 71% hôm nay đến từ verify runtime độc lập trên 18 domain,
không phải suy diễn lại từ cùng 1 nguồn — sự trùng khớp là tín hiệu tốt, không phải trùng lặp.)

## Bảng đầy đủ 18 domain

| # | Nhóm | Domain | Trạng thái | % | Bằng chứng chính | Gap cụ thể |
|---|---|---|---|---|---|---|
| 1 | NÃO BỘ | omni-analyst | ĐANG DỞ | 80 | Pod `omni-fullstack` log 24h: 2 lượt LLM advisory OK + 5 `ADVISORY_DECISION` CRAT-audited thành công; RAG-gate MISS + deterministic `META_SELF` path chạy đúng thiết kế; 710/710 test pass | `make benchmark-advisory` (100pt, pass=70) chưa có kết quả gần đây được xác nhận trong cluster; chưa quan sát case URGENT/CRITICAL đi qua L1_AUTO/L3_HITL thật trong log mẫu |
| 2 | NÃO BỘ | omni-rag | ĐANG DỞ | 55 | Redis `omni:rag:sop` HLEN=1019 nhưng đây là hash phẳng KHÔNG PHẢI collection HNSW `itops_sop_ledger` (0 docs) mà pipeline thật query lúc runtime; 5/11 collection rỗng; semantic cache 0 hit (chưa từng dùng); playbook store chỉ 2 entry; 614/614 test pass | Fix mismatch `omni:rag:sop` (legacy) vs `itops_sop_ledger` (HNSW thật) — analyst hiện KHÔNG recall được SOP nào qua vector search dù MEMORY.md báo "1019"; populate 5 collection rỗng; kích hoạt semantic cache |
| 3 | NÃO BỘ | omni-autonomy | ĐANG DỞ | 65 | Tier hiệu lực `shadow`, kill-switch `false` — đúng khai báo; không tìm thấy đường bypass tier/kill-switch nào ngoài nguồn hợp lệ Redis>PG>env; 213/213 test pass | Nhánh SIEM (`_is_siem_batch`) hoàn toàn không gọi `tier_gate`/`resolve_tier`/`gate_decision_for_tool` — governance gap thật (khớp #13), không phải false positive |
| 4 | NÃO BỘ | omni-core | ĐANG DỞ | 45 | `three_sigma`/`baseline_snapshot` SỐNG THẬT (timestamp cách hiện tại ~2 phút, z-score window có data thật); 473/473 test logic pass | `forecast_autonomous_loop` chạy nhưng LUÔN lỗi 400 (Prometheus query dùng literal `now-24h` thay vì epoch); `deep_scout` chạy nhưng fail-silent (Redis timeout, topology không persist); `proactive_react_runner` chỉ connect Kafka, 0 log evaluate/incident trong 48h (dormant) |
| 5 | ĐỘI SRE ẢO | omni-remote-agent | ĐANG DỞ (gần DONE) | 88 | 3/3 VM `aoip-agent.service` active thật; `omni-remote-agent.service` cũ xác nhận disabled đúng thời điểm follow-up round hôm nay; `_resolve_trusted_binary()` + `mysqladmin` allowlist chặt verify khớp code thật; provisioner/tunnel process sống thật; 176/176 test pass | `aoip.agent.daemon` (canonical target ADR-001) vẫn chỉ demo/proof script, CHƯA deploy thật; security fix chưa PoC lại trên VM Linux thật qua `orb -m` |
| 6 | VẬN HÀNH & GOVERNANCE | omni-executor | ĐANG DỞ | 65 | Path chính `omni-fullstack`: RBAC đúng thiết kế (không cluster-admin, chỉ patch/update secrets qua ClusterRole gate riêng), `MUTATE_TOOL_ALLOWLIST` khớp skill, 361/361 test pass, 0 mutation log = đúng vì kill-switch (không phải bug) | **CRITICAL**: 3 ClusterRoleBinding cluster-admin legacy (`omni-worker`/`omni-analyst`/`omni-prober`) vẫn sống, `omni-worker` đang ACTIVE dùng bởi 2 CronJob thật (`crat-integrity-check` hourly, `knowledge-ingest` weekly) — vi phạm trực tiếp invariant "executor NEVER cluster-admin" |
| 7 | VẬN HÀNH & GOVERNANCE | omni-crat | DONE | 95 | `OMNI_AUDIT_PRIVATE_KEY_PATH` set thật, PEM Ed25519 hợp lệ qua K8s Secret mount; 5 block gần nhất có `signature_hex` 128-hex-char thật; `head_hash` khớp block mới nhất; CronJob integrity-check OK 49 phút trước; 86/86 test pass | Không có gap lớn — SIGNED thật, production-grade |
| 8 | VẬN HÀNH & GOVERNANCE | omni-hitl | CHƯA LÀM (vận hành) | 35 | Code + 125/125 test pass đầy đủ theo thiết kế; Telegram secret cấu hình thật (2 key hợp lệ) | 0/300-5000 dòng log gần đây có dấu vết HITL; consumer group `omni-hitl-dispatcher` KHÔNG active trong `kafka-consumer-groups --list`; topic `omni-hitl-pending` chỉ 2 offset lịch sử — dispatcher chưa chứng minh chạy sống |
| 9 | VẬN HÀNH & GOVERNANCE | omni-observability | ĐANG DỞ | 55 | `/healthz`/`/readyz` 200 OK; `/metrics` đúng port 9090 (86 metric `omni_*`, không phải :8090 như doc mô tả — lệch tài liệu); 303/303 test pass | Redis `omni:kpi:z:accepted`/`false_positive` RỖNG → acceptance-rate/false-positive-rate luôn 0, alert rule nguy cơ vô nghĩa; `omni_health_check_status` gauge luôn=1.0 bất kể trạng thái (khả năng bug mapping); benchmark advisory 100pt không có report gần đây |
| 10 | VẬN HÀNH & GOVERNANCE | omni-prober | DONE | 90 | Log live: dispatcher publish evidence thật, alert dedup đang chặn trùng thật (2 dedup key sống), circuit breaker/delayed queue đúng trạng thái (không trip vì không có backpressure); 98/98 test pass | Không có gap lớn |
| 11 | VẬN HÀNH & GOVERNANCE | omni-kafka-pipeline | ĐANG DỞ (drift tài liệu) | 65 | Traffic thật xác nhận (`omni-diagnostic-evidence` offset=7371, LAG=0 mọi topic chính); 48/48 test pass | CLAUDE.md claim sai: `omni-knowledge-evidence` thực tế 1 partition (không phải 3 — script `--if-not-exists` no-op không alter được); chỉ 1/3 topic SIEM đạt 6 partition; 18 consumer group thật (không phải 12 — gồm `brain-go-kafka` retired chưa dọn + 5 group rác test); DLQ code tồn tại nhưng chưa từng nhận message |
| 12 | VẬN HÀNH & GOVERNANCE | omni-gateway | DONE | 95 | `grep "from workers"` trong `src/gateway/` = 0 hit (import boundary sạch); health 200 thật; auth 401 thật khi thiếu API key; 196/196 test pass | Không có gap lớn |
| 13 | VẬN HÀNH & GOVERNANCE | omni-siem | ĐANG DỞ | 55 | brain-go RETIRED xác nhận (0 deploy/pod); `OMNI_SIEM_CORRELATION_ENABLED=true`, không đua song song với brain-go; offset đã KHÔNG còn =0 (`omni-siem-raw`=107, `chains`=5, LAG=0) — nhưng đây là **parity test data**, không phải traffic FinGuard sản xuất thật; 108/108 test pass | Escalation tier gate cho nhánh SIEM VẪN CHƯA implement (comment ghi ý định, code chưa làm — khớp #3) — xác nhận lại hôm nay, chưa fix; chưa có traffic sản xuất thật |
| 14 | CHẤT LƯỢNG & HẠ TẦNG | omni-testing | DONE | 92 | Full suite **6550 passed, 0 failed** (202s); coverage gate 90% enforce thật qua `.coveragerc.gate` + Makefile target, không phải tài liệu suông | `tests/integration/` chỉ còn README + `.pyc` cache, file test thật đã biến mất khỏi disk — chưa rõ lý do, cần điều tra |
| 15 | CHẤT LƯỢNG & HẠ TẦNG | omni-lane-operator-loop | ĐANG DỞ | 89 | Rubric B3 (skill gốc, KHÔNG tự chế thang mới) áp lên ledger: SYS_RESOURCE + SYS_HARD_FAIL 12/12 stage ổn định tới iter27, tier=`shadow` xác nhận sống | Lane APP_HTTP theo skill được đánh dấu "bỏ qua — chưa hoàn thiện", không có dữ liệu vận hành nào; carry-over F28/F26/F31/F22/F27/F16/F5-minor vẫn mở (đa số model-ceiling qwen2.5-coder:7b, không phải bug) |
| 16 | CHẤT LƯỢNG & HẠ TẦNG | Runtime THẬT (kubectl/orb) | DONE | 92 | 13/16 pod Running đúng topology CLAUDE.md; tier/kill-switch/RAG-HLEN/brain-go-retired đều khớp 100% sau 44 ngày | 2 drift nhỏ chưa ghi tài liệu: Deployment `nginx-test` lạ (tạo hôm nay, không rõ mục đích), CronJob `crat-integrity-check` không có trong bảng topology |
| 17 | CHẤT LƯỢNG & HẠ TẦNG | Portal/UI | ĐANG DỞ | 50 | 4 pod (provider-web/portal, tenant-web/portal) Running thật; Capability Matrix 8/15 ✅ operator-visible (đa số chỉ qua API, chưa UI thân thiện chuẩn ADR-003) | Advisory chi tiết (WHAT/WHO/WHY/HOW-TO/Forecast) CHỈ hiển thị ở provider-portal (nội bộ Omni); tenant-portal (khách hàng thật) chỉ có Twin tóm tắt + incident list trơ — CHƯA đạt "não bộ khách hàng nhìn thấy được" |
| 18 | CHẤT LƯỢNG & HẠ TẦNG | Security/RBAC toàn hệ thống | DONE (kèm 1 cross-ref) | 80 | Kill-switch=`false` xác nhận thật ngay lúc audit (không tái diễn drift 2026-06-11); 10 secret hợp lệ, không secret lạ/hardcode; RBAC `omni-fullstack` không wildcard nguy hiểm; tenant isolation VERIFIED_RUNTIME (PRODUCT_PROOF iter 9) | Cross-ref #6: 3 ClusterRoleBinding cluster-admin legacy vẫn sống cho SA khác (`omni-worker` v.v.) — cùng 1 gap, khác domain phát hiện; UX gap `resolve_scope()` silent-override (không phải security hole, đã ghi backlog) |

## Cách tính % tổng thể (trọng số công khai, không giấu)

| Nhóm | Domain # | Trọng số/domain | Tổng trọng số nhóm | % trung bình nhóm | Đóng góp vào tổng |
|---|---|---|---|---|---|
| NÃO BỘ (reasoning core) | 1-4 | 7.5% | 30% | 61.25% | 18.4 |
| ĐỘI SRE ẢO (field executor) | 5 | 15% | 15% | 88.0% | 13.2 |
| VẬN HÀNH & GOVERNANCE (backbone an toàn) | 6-13 | 5% | 40% | 69.4% | 27.8 |
| CHẤT LƯỢNG & HẠ TẦNG (nền chất lượng) | 14-18 | 3% | 15% | 80.6% | 12.1 |
| **TỔNG** | | | **100%** | | **≈ 71%** |

**Lý do trọng số**: NÃO BỘ + ĐỘI SRE ẢO = 45% vì đây là 2 trụ cột định nghĩa trực tiếp vision "bộ
não SRE trung tâm + đội SRE ảo" — không có 2 nhóm này thì không có gì để gọi là "Autonomous SRE".
VẬN HÀNH & GOVERNANCE = 40%, chia đều 8 domain vì đều là invariant/governance ngang hàng bắt buộc
để "tự vận hành 24/7/365 AN TOÀN" (audit/HITL/tier/kafka/gateway/siem/executor/observability) —
không domain nào trong nhóm này là phụ, thiếu 1 domain là thiếu 1 lớp phòng thủ. CHẤT LƯỢNG & HẠ
TẦNG = 15% thấp nhất vì đây là nền tảng hỗ trợ (test/rubric/runtime-drift/portal/security-audit),
quan trọng nhưng không trực tiếp là "năng lực SRE tự động" — ngoại lệ Portal/UI (#17) đáng lẽ có
thể xếp trọng số cao hơn vì là mặt "khách hàng nhìn thấy" của vision, nhưng giữ 3% đồng nhất theo
nhóm cho minh bạch, và ghi rõ ở top-3 domain yếu nhất bên dưới thay vì thổi trọng số riêng.

## Top 3 domain yếu nhất

1. **omni-hitl (35%)** — code+test đầy đủ (125 pass) nhưng 0 bằng chứng dispatcher chạy thật gần
   đây; consumer group không active; escalation path an toàn con người chưa được chứng minh sống.
2. **omni-core (45%)** — baseline/3-sigma sống thật, nhưng forecast loop luôn lỗi (Prometheus
   query sai định dạng epoch), deep_scout fail-silent (Redis timeout), proactive ReAct dormant
   48h (chỉ connect Kafka, không evaluate).
3. **Portal/UI (50%)** — advisory chi tiết (WHAT/WHO/WHY/HOW-TO/Forecast) chỉ hiển thị ở
   provider-portal (nội bộ Omni); tenant-portal (khách hàng thật) chỉ có Twin tóm tắt + incident
   list trơ — CHƯA phải "não bộ khách hàng nhìn thấy được" như vision đòi hỏi.

*(Sát nút phía sau: omni-rag 55%, omni-observability 55%, omni-siem 55%.)*

## Đối chiếu chéo với gap đã biết

**3 gap Vision V2** (§2 C1/C2/C3 bên dưới, viết 2026-06-29) — vẫn đúng, ma trận 18 domain hôm nay
KHÔNG mâu thuẫn, chỉ bổ sung bằng chứng runtime:
- **C1 Knowledge phi-graph** → phản ánh gián tiếp ở #2 omni-rag (55%: vẫn chỉ vector phẳng, 5/11
  collection rỗng, semantic cache 0 hit). Không có Knowledge Graph nào trong 18 domain vì nó CHƯA
  TỒN TẠI — đúng như Vision V2 đã ghi nhận từ đầu, chưa build (không phải regression).
- **C2 Onboarding sơ khai** → không map trực tiếp vào domain nào trong 18 (onboarding có audit
  riêng ở `PRODUCT_PROOF.md`: Twin/Competency/Unknowns VERIFIED_RUNTIME một phần, UI
  `/understanding` tồn tại). #17 Portal/UI audit tenant-portal khách hàng, khác phạm vi operator
  onboarding UI.
- **C3 Remote Agent = collector** → #5 (88%) xác nhận remote agent đã tiến hoá xa hơn "collector"
  thuần (durable command channel, security guard chặt, provisioning tự động) nhưng vẫn CHƯA có
  local reasoning/verify-before-believe — đúng lộ trình R2 mà Vision V2 đã đề xuất, chưa bắt đầu.

**ADR-001 tiến độ**: #5 xác nhận `aoip.agent.employee` (bản trung gian) chạy thật 3/3 VM,
`aoip.agent.daemon` (canonical target dài hạn) VẪN chỉ demo/proof script, chưa deploy — khớp
chính xác trạng thái ADR-001 đã ghi ("việc triển khai thật để phase sau", chưa có ADR/kế hoạch
runtime-verify tiếp theo).

## Roadmap đóng gap — 18 domain, xếp effort vs impact

**P0 — Effort thấp, impact cao (làm ngay):**
| Domain | Gap | Effort | Impact |
|---|---|---|---|
| #6, #18 | Xoá/thu hẹp 3 ClusterRoleBinding cluster-admin legacy (`omni-worker`/`omni-analyst`/`omni-prober`) — đang ACTIVE, vi phạm invariant bảo mật | S | CAO |
| #8 | Verify + khôi phục omni-hitl dispatcher chạy sống (deploy/wire consumer group, inject 1 test HITL thật) | S-M | CAO |

**P1 — Effort vừa, impact cao (đợt tới):**
| Domain | Gap | Effort | Impact |
|---|---|---|---|
| #3, #13 | Wire `tier_gate`/escalation cho nhánh SIEM (1 fix dùng chung cho cả 2 domain) | M | CAO |
| #2 | Fix mismatch `omni:rag:sop` vs `itops_sop_ledger`; populate 5 collection rỗng; kích hoạt semantic cache | M | CAO |
| #9 | Fix KPI `accepted`/`false_positive` write path; chạy `make benchmark-advisory` lấy điểm thật; fix gauge `omni_health_check_status` | S-M | TRUNG BÌNH |
| #4 | Fix forecast Prometheus query format (epoch); fix deep_scout Redis timeout; wire proactive loop để thật sự evaluate | S-M | TRUNG BÌNH-CAO |

**P2 — Effort lớn, impact cao, cần quyết định sản phẩm/kiến trúc (tháng tới):**
| Domain | Gap | Effort | Impact |
|---|---|---|---|
| #17 | Quyết định + implement hiển thị Advisory/WHY/HOW-TO cho tenant-portal khách hàng — đây chính là "não bộ khách hàng nhìn thấy được" theo vision, hiện chỉ có ở provider-portal nội bộ | L | CAO |
| #5 | Quyết định tiếp tục ADR-001 (`aoip.agent.daemon` deploy thật) hay dừng ở `employee` lâu dài | L | TRUNG BÌNH |
| #15 | Quyết định roadmap lane APP_HTTP (hiện bỏ qua, 0 dữ liệu vận hành) | L | TRUNG BÌNH |

**P3 — Dọn dẹp/hygiene (khi rảnh):**
| Domain | Gap | Effort | Impact |
|---|---|---|---|
| #11 | Fix script Kafka partition provisioning (ALTER thật thay vì `--if-not-exists` no-op); dọn 6 consumer group rác | S | THẤP-TRUNG BÌNH |
| #14 | Điều tra + khôi phục file test `tests/integration/` đã biến mất khỏi disk | S | THẤP-TRUNG BÌNH |
| #16 | Cập nhật CLAUDE.md ghi nhận `nginx-test` Deployment + CronJob `crat-integrity-check` vào bảng topology | S | THẤP |
| #18 | Fix UX gap `resolve_scope()` silent-override (không phải security hole, chỉ UX) | S | THẤP |

---

# Architecture Assessment — Autonomous SRE Framework (V2)

> **Status**: ASSESSMENT ONLY — không refactor, không đổi code.
> **Created**: 2026-06-29
> **Author**: Chief Software Architect (analysis pass)
> **Scope**: So sánh kiến trúc Omni hiện tại với vision "Autonomous SRE thay thế Senior SRE team".

---

## 0. TL;DR

Omni hiện tại là một **reactive incident-remediation pipeline** rất trưởng thành (4 lane chẩn đoán, RAG→LLM→CRAT→HITL→executor, audit hash-chain, autonomy tier, Telegram VI). Vision mới đòi hỏi một **lifecycle-driven SRE worker** (Phase 0→4) với mental-model "nhân viên SRE mới tiếp nhận hệ thống".

**Kết luận cốt lõi**: ~70% nền tảng có thể giữ nguyên. Khoảng cách lớn nhất KHÔNG phải reasoning/execution (đã tốt) mà là **tầng tri thức (knowledge layer)**:

1. **Không có Knowledge Graph** — tri thức nằm rời rạc ở RAG vector + Redis key, không có quan hệ first-class (Customer→Host→Service→API→DB→Owner). Đây là gap #1.
2. **Onboarding mới ở mức sơ khai** — discovery có, nhưng chưa có vòng "đọc doc → verify fact → hỏi human → cập nhật understanding → biết mình KHÔNG biết gì". Đây là gap #2.
3. **Remote Agent vẫn là collector**, chưa phải "SRE worker" (chưa có verify-before-believe, confidence per-fact, local reasoning). Gap #3.

KHÔNG cần rewrite. Cần **bổ sung một Knowledge/Twin plane** lên trên pipeline hiện có, và **nâng cấp Remote Agent + Onboarding** theo lifecycle.

---

## 1. Phần kiến trúc ĐÃ KHỚP vision (giữ nguyên)

| Vùng | Hiện trạng | Khớp nguyên tắc |
|---|---|---|
| Reasoning plane (analyst) | RAG gate (deterministic) → LLM (last resort) → AnalystAdvisory schema | ✅ "LLM is last, not first"; "không nhét business logic vào prompt" — `diagnostic_policy.py` giữ invariant bằng code |
| Execution plane | executor tách biệt, MUTATE_TOOL_ALLOWLIST, kill-switch fail-closed, post-mutate verify | ✅ "mutations only via executor", explainable, RBAC |
| CRAT audit | SHA-256 hash-chain + Ed25519, fail-closed trước mọi emit | ✅ "every action explainable", regulatory |
| Event model | Kafka split topics (alerts/evidence/actions/feedback/audit/knowledge) | ✅ "everything is an event" — đã event-driven thật |
| Autonomy tier | shadow/minimal/autonomous, Redis>PG>env, effective=min(tier, confidence) | ✅ graduated autonomy — đúng hướng "replace myself dần dần" |
| Verify loops | `post_mutate_sdk_verify`, `alert_sdk_truth_compare` (alert claim vs SDK ground truth) | ✅ "verify before believe" — đã có mầm mống ở phía diagnosis |
| Knowledge routing | `INV_KNOWLEDGE_NOT_ALERT`: non-ANOMALY tách khỏi diagnostic pipeline | ✅ tách "học" khỏi "chữa cháy" — đúng mental model |
| Confidence→autonomy | `remote_host_baseline.py` ConfidenceLevel + decay | ✅ "knowledge có confidence", evolves over time |

**Đây là tài sản. Vision message #2 nói đúng: assume valuable unless proven otherwise.**

---

## 2. Phần XUNG ĐỘT với vision

| Xung đột | Mô tả | Mức |
|---|---|---|
| **C1 — Knowledge phi-graph** | Tri thức = RAG vectors (9 collections) + Redis keys phẳng. Không có node/edge, không truy được "API X phụ thuộc DB Y thuộc owner Z". Vision đòi GRAPH FIRST. | CAO |
| **C2 — Onboarding ≠ "Senior SRE mới"** | `pkg/onboarding/discovery_doc.py` + collectors chỉ snapshot trạng thái. Thiếu vòng lặp: hypothesis→verify→ask-human→knowledge update; thiếu "biết mình không biết". | CAO |
| **C3 — Remote Agent = collector** | `agent.py` chạy fixed lanes mỗi 60s, đẩy evidence. Không reason cục bộ, không verify trước khi tin doc/config, không sinh câu hỏi "không lười". | TRUNG BÌNH |
| **C4 — Discovery hardcode, chưa plugin** | Collectors là module cố định (mysql/proxysql/haproxy/k8s...). Vision đòi "plugins over hardcode" cho từng vendor (AWS/Azure/VMware/Confluence/ArgoCD...). | TRUNG BÌNH |
| **C5 — Lifecycle ngầm định** | Code xoay quanh "alert→remediation". Vision xoay quanh "customer lifecycle Phase 0–4". Phase 1 (onboarding) và Phase 3 (continuous improvement) gần như chưa có khung. | TRUNG BÌNH |
| **C6 — Data residency vs Twin** | Vision: Omni chỉ giữ experience/patterns, KHÔNG giữ infra khách (`INV_DATA_RESIDENCY` — doc chỉ metadata). Nhưng "Digital Twin" cần một bản đồ topology sống. → Twin phải sống **ở phía khách hàng/agent**, Omni chỉ giữ reference + experience. Hiện chưa phân định rõ ranh giới này. | CAO (thiết kế) |

---

## 3. Phần GIỮ NGUYÊN (không động tới)

- `services/audit_ledger/` (CRAT) — hoàn chỉnh, regulatory-grade.
- `pkg/reasoning/` schema + `diagnostic_policy.py` invariants — đây là "business logic ngoài prompt", đúng vision.
- Kafka transport (`messaging/kafka_bus.py`) + topic map.
- Execution plane (`execution/`, `pkg/executor/`, executor role).
- Autonomy tier machinery (PG `omni_admin`, resolve_tier cache).
- smart-siem Go services (brain-go/agent/bff) — pipeline song song độc lập, không cản vision.

---

## 4. Phần cần TIẾN HÓA (evolve, không thay)

| Thành phần | Tiến hóa thành |
|---|---|
| RAG vector store (`rag/`) | Vẫn giữ cho similarity recall, **nhưng** trở thành "index phụ" cạnh Knowledge Graph (graph là source-of-truth quan hệ, vector là recall). |
| Remote Agent (`remote_agent/`) | Từ collector → **SRE worker**: thêm local reasoning nhẹ, verify-before-believe, per-fact confidence, sinh câu hỏi giàu ngữ cảnh. |
| `pkg/onboarding/` | Từ snapshot → **Onboarding state machine** (Phase 1): đọc doc → dựng hypothesis → verify → hỏi human → cập nhật graph → đo "độ hiểu". |
| `knowledge_pipeline.py` | Từ dispatcher Redis → **graph upsert**: METRIC/LOG/DISCOVERY/CHANGE → node/edge mutation trên twin. |
| ConfidenceLevel (per-host) | Mở rộng thành **confidence per-fact/per-edge** trên graph, không chỉ per-host. |
| `proactive_observer` | Hạt giống của Phase 3 (Continuous Improvement) — mở rộng từ "fix anomaly" sang "đề xuất cải tiến HA/cost/coverage". |

---

## 5. Subsystem MỚI cần có

1. **Knowledge Graph plane** (gap #1) — node types: Customer/Site/Cluster/Namespace/Node/Pod/Service/API/DB/Topic/Bucket/Firewall/Owner/BusinessCapability/Runbook/Incident/Change. Edge first-class. Lưu **per-tenant ở phía khách** (residency), Omni giữ reference + schema.
2. **Digital Twin sync** — incremental sync từ agent → graph (Principle: no full scans, incremental). Reality thay đổi → twin thay đổi.
3. **Onboarding engine** (Phase 1) — vòng observe→hypothesize→verify→ask→learn, với metric "coverage / unknown count".
4. **Human-learning loop** — câu hỏi "không lười" (đã-biết + hiểu-hiện-tại + bất-định + câu-hỏi-chính-xác); answer → graph update; never ask twice.
5. **Discovery plugin registry** — mỗi capability (AWS/VMware/Confluence/ArgoCD/Prometheus/DNS...) là plugin theo 1 interface, thay cho collector hardcode.
6. **Continuous Improvement engine** (Phase 3) — proactive review (architecture/capacity/security/cost/coverage gaps).
7. **Experience store tách khỏi customer knowledge** (Phase 4) — Omni global chỉ chứa reasoning patterns/playbooks/lessons; customer infra ở lại tenant.

---

## 6. Abstraction hiện SAI / lệch

- **"Evidence" gánh quá nhiều vai**: vừa là alert proof, vừa là knowledge sample, vừa là discovery. Đã tách topic nhưng cùng một envelope shape — nên tách contract: `DiagnosticEvidence` vs `KnowledgeObservation` vs `TopologyFact`.
- **Collector = capability** bị trộn: collector vừa thu thập vừa quyết định criticality. Nên tách "discover" (plugin) khỏi "judge criticality" (reasoning).
- **Confidence chỉ per-host**: tri thức thực tế có độ tin khác nhau theo từng fact (doc nói A, config nói B). Cần per-fact.
- **Onboarding là worker role phụ** (`omni-onboarding` pod): vision coi onboarding là Phase QUAN TRỌNG NHẤT — nên là first-class engine, không phải side worker.

---

## 7. Folder structure — còn phù hợp không?

**Phần lớn còn tốt** (đã chia theo domain: workers/gateway/remote_agent/services/rag/pkg). KHÔNG cần đại phẫu như "Constitution V1" (control-plane/discovery-plane/... 17 thư mục) — đó là over-engineering với codebase đã chạy.

Đề xuất bổ sung **tối thiểu**, không phá vỡ:
- `src/graph/` — knowledge graph model + store (mới).
- `src/discovery/plugins/` — di chuyển dần `remote_agent/collectors/` về interface plugin (giữ backward-compat).
- `src/services/onboarding_engine/` — nâng `pkg/onboarding` thành engine.
- Giữ nguyên mọi thứ khác.

→ **Evolution, không revolution.** Mâu thuẫn giữa hai vision message (V1 đòi đập đi xây lại 17-plane; V2 nói "đừng rewrite") — chọn V2.

---

## 8. Event model — còn phù hợp không?

✅ **Có, rất phù hợp.** Kafka split-topic + envelope trace_id là nền event-driven tốt. Chỉ cần thêm event types mới (graph mutation, twin-diff, onboarding-question, fact-verified) — không đổi transport. `kafka_knowledge_evidence_loop` đã là chỗ neo cho graph upsert.

---

## 9. Planner / Policy / Worker — có cần tiến hóa?

- **Planner** (analyst_agentic_loop, ReAct) → nâng từ "plan remediation" sang "plan mission" (Chief SRE giao mission cho agent). Giữ cơ chế, mở rộng scope.
- **Policy** (diagnostic_policy, env_mode, autonomy tier) → giữ; thêm policy "fact phải verified mới được dùng để mutate".
- **Worker = Remote Agent** → tiến hóa mạnh nhất (xem §4). Omni = Chief (plan/reason/reflect/learn); Agent = field SRE (discover/observe/execute/verify/ask/report). Phân vai này ĐÚNG và codebase đã gần — chỉ cần dịch chuyển reasoning nhẹ xuống agent và mission-planning lên Omni.

---

## 10. Knowledge Graph — có cần không, ở đâu?

**CẦN — đây là trục xương sống của vision** (digital twin, dependency, "hiểu WHY không chỉ WHAT").

Vị trí (giải quyết xung đột residency C6):
- **Graph dữ liệu khách (twin thật)**: sống **per-tenant ở phía khách hàng** (cạnh agent / trong cluster khách). Chứa node/edge thật.
- **Omni global**: chỉ giữ **graph schema + reference + experience patterns** (anonymized). KHÔNG copy infra khách lên Omni.
- Vector RAG → demote thành index recall cạnh graph.

Công nghệ: bắt đầu nhẹ (graph-in-Redis hoặc networkx + persist) trước khi cân nhắc Neo4j — tránh hạ tầng nặng sớm.

---

## 11. Remote Agent tiến hóa từ hôm nay thế nào

Hiện tại (`agent.py`): discovery on-start → derive collectors → loop 60s đẩy evidence + poll command. Tốt làm nền.

Lộ trình tiến hóa (giữ loop, thêm năng lực):
1. **Verify-before-believe**: khi đọc doc/config, không tin ngay → đối chiếu runtime (port thật mở? service thật chạy?) → gắn confidence per-fact.
2. **Local triage nhẹ**: agent tự phân loại "biết / nghi ngờ / không biết" trước khi đẩy lên.
3. **Question generation**: khi gặp UNKNOWN_ENTITY → sinh câu hỏi giàu ngữ cảnh (đã-biết + hiểu + bất-định + hỏi).
4. **Plugin collectors**: chuyển `collectors/` sang interface plugin để thêm vendor không sửa core.
5. **Twin reporter**: emit TopologyFact (node/edge) thay vì chỉ raw evidence.

---

## 12. Migration strategy (an toàn, incremental)

- **Strangler pattern**: Graph plane chạy SONG SONG, đọc từ `omni-knowledge-evidence` đang có. Không đụng diagnostic pipeline.
- **Dual-write**: knowledge_pipeline vừa giữ Redis key cũ vừa upsert graph → so sánh → cutover khi graph đủ tin.
- **Contract-first**: định nghĩa `TopologyFact` / graph schema trước, agent emit dần.
- **Onboarding engine** dựng cạnh worker `onboarding` hiện có, không thay.
- KHÔNG đụng CRAT / executor / autonomy tier trong giai đoạn đầu.

---

## 13. Rủi ro

| Rủi ro | Mức | Giảm thiểu |
|---|---|---|
| Data residency: graph vô tình copy infra khách lên Omni | CAO | Enforce `INV_DATA_RESIDENCY` ở graph store; twin sống phía khách |
| Over-engineering theo Constitution V1 (17-plane) | CAO | Bám V2: evolve, đo bằng giá trị thật |
| LLM quality (qwen2.5-coder:7b 7B) cho mission-planning | TRUNG BÌNH | Giữ deterministic-first; LLM chỉ khi RAG miss (carry-over F28/F31 model-ceiling) |
| Graph + vector double source-of-truth lệch nhau | TRUNG BÌNH | Graph = truth quan hệ, vector = recall; one-way sync |
| Onboarding hỏi human quá nhiều → mệt mỏi | TRUNG BÌNH | "never ask twice", batch câu hỏi, ưu tiên theo criticality |
| Agent local reasoning làm nặng host khách | THẤP | Giữ triage nhẹ, reasoning nặng vẫn ở Omni |

---

## 14. Roadmap đề xuất (theo lifecycle, không theo plane)

- **R0 — Contract & Graph schema** (nền): định nghĩa node/edge, `TopologyFact`, residency boundary. Không code pipeline.
- **R1 — Twin plane (read-only)**: graph store + upsert từ `omni-knowledge-evidence` (dual-write, strangler). Visualize twin.
- **R2 — Remote Agent → SRE worker**: verify-before-believe + per-fact confidence + plugin collectors.
- **R3 — Onboarding engine (Phase 1)**: observe→hypothesize→verify→ask-human→learn; metric coverage/unknown.
- **R4 — Human-learning loop**: câu hỏi không-lười + answer→graph; never-ask-twice.
- **R5 — Mission planner (Phase 2 nâng cấp)**: Chief SRE giao mission, agent thực thi + report.
- **R6 — Continuous Improvement (Phase 3)**: proactive review (HA/cost/capacity/coverage).
- **R7 — Experience store (Phase 4)**: tách global patterns khỏi customer knowledge; reflection loop.

Mỗi R đứng độc lập, có giá trị riêng, không bắt buộc làm hết.

---

## 15. Nguyên tắc chỉ đạo (chốt mâu thuẫn V1 vs V2)

> Vision message #1 (Constitution V2 "đập đi xây lại") và message #2 ("đừng rewrite, hiểu trước") mâu thuẫn về **mức độ**.
> **Chọn message #2 làm kim chỉ nam**: Omni đã là production system trưởng thành. Tối ưu cho 10 năm tới = **bổ sung knowledge/twin plane đúng + nâng cấp agent theo lifecycle**, KHÔNG phá nền pipeline/CRAT/executor đang chạy tốt.
> "Never optimize bad architecture, replace it" chỉ áp dụng cho **tầng knowledge phi-graph (C1)** và **onboarding sơ khai (C2)** — đó là chỗ thật sự cần xây mới.
