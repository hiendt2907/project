# Omni v2 — RED TEAM Architecture Review

**Vai trò:** Principal Architect, red team — nhiệm vụ là BÁC BỎ, không phải phê duyệt · **Ngày:** 2026-08-03
**Input bị nghi ngờ:** `OMNI_V2_ARCHITECTURE_REDESIGN.md`, `OMNI_AUTONOMOUS_AGENT_PLATFORM_REVIEW.md`, `OMNI_V2_IMPLEMENTATION_PLAN.md` — cả 3 do CHÍNH tôi viết ở các lượt trước trong phiên này. Không có ngoại lệ "tự tin vì mình vừa viết" — verify lại bằng lệnh thật (`grep`/`wc -l`/`git log`), không đọc lại kết luận cũ.

---

## 0. Bằng chứng xác minh — 3 tài liệu trước SAI ở đâu

| Claim trong tài liệu trước | Xác minh thật (lệnh chạy) | Kết quả |
|---|---|---|
| "System Twin (`aoip`) chỉ dùng trong 2 script demo, chưa cắm dây vào production" (Đ14) | `grep -rln "from aoip\|import aoip" src/workers src/gateway src/pkg src/services` | **SAI.** 7 module production import `aoip` thật: `workers/onboarding_pipeline.py`, `workers/auto_recovery_bridge.py`, `workers/system_twin_context.py`, `gateway/routes/onboarding.py`, `gateway/routes/agent_runtime.py`, `pkg/onboarding/discovery_doc.py`, `services/agent_command_ledger/ledger.py`. `system_twin_context.py` render Twin thành block evidence, **CÓ inject vào `evidence_consumer.py`** (advisory reasoning) — Twin đã là ngữ cảnh sống cho LLM, không phải demo |
| "`aoip` là module thí điểm, gần như bị bỏ quên" (ngụ ý ở Đ14) | `git log --oneline -- src/aoip/ \| wc -l` = 58 commit; commit gần nhất "5 giờ trước" (2026-08-03); `find tests -iname "*aoip*"` = **95 file test** | **SAI.** Đây là subsystem đang phát triển tích cực, có test surface lớn hơn nhiều module "chính thức" khác trong 3 tài liệu trước |
| "`SystemModel.blast_radius()` 0 call site production" (Đ14) | `grep -rn "SystemModel.blast_radius\|\.blast_radius(" src/workers src/gateway src/pkg/executor` | **ĐÚNG, nhưng hẹp hơn nhiều so với ấn tượng "aoip là demo" mà Đ14 tạo ra.** Chỉ riêng hàm BFS này chưa được gọi — không có nghĩa cả `aoip` là code chết |
| "5 quyết định autonomy rải rác 6 file" là vấn đề cần fix bằng 1 API tập trung (Đ13/Đ14) | `grep -rln "OMNI_AUTO_EXECUTE_ENABLED\|auto_execute_enabled" src/workers/*.py src/pkg/**/*.py` | Thật ra là **7 file**, không phải 6. Nhưng đây KHÔNG chắc là bug — xem mục 3 |
| Ngụ ý "Governance/Autonomy chưa tách là thiếu sót cần 2 Engine mới" (Đ14) | `wc -l src/pkg/executor/mutate_governance.py src/pkg/autonomy/tier_gate.py` = 124 + 206 dòng, đã là 2 file riêng từ trước | **Đã tách rồi ở mức module.** Cái thiếu chỉ là 2 file rác (`gate.py`/`policy.py`, 369 dòng, **1 call site duy nhất**: `gateway/routes/autonomy.py`) — xem mục 4 |
| (Không có trong 3 tài liệu trước — phát hiện MỚI của red team) | `grep -n "teardown-omni-postgres" Makefile` + đọc `scripts/teardown_omni_postgres.sh` | **Landmine thật đang sống trong repo**: script scale `omni-worker`/`omni-watchdog` (2 Deployment ĐÃ RETIRED từ commit `915e509`) rồi xoá `cluster.postgresql.cnpg.io/omni-postgres` — nhưng `omni-postgres.yaml` hiện tại comment rõ "source-of-truth cho Admin config schema omni_admin", và `OMNI_ADMIN_PG_DSN` trong `omni-fullstack`/`omni-gateway` trỏ đúng cluster này. Script viết cho lý do "RAG đã chuyển sang Redis Stack" — đúng cho RAG, nhưng KHÔNG đúng cho `omni_admin` (agent_credential, tenant config, autonomy tier — dữ liệu sống, 14+ migration). Ai chạy `make teardown-omni-postgres APPLY=1` hôm nay sẽ xoá nhầm DB đang phục vụ production lab |

**Kết luận mục 0:** cả 3 tài liệu trước có ít nhất 1 lỗi định tính nghiêm trọng (đánh giá sai độ trưởng thành của `aoip`) và bỏ sót 1 rủi ro sống thật không liên quan gì đến v2 nhưng ảnh hưởng trực tiếp tới 3 workstream định thêm bảng vào cùng Postgres đó (WS3, WS4, WS8).

---

## 1. Workstream Validation (WS0-WS10)

| WS | Cần thiết? | Verdict |
|---|---|---|
| WS0 (import-linter) | Có — nhưng không cấp bách, chỉ là công cụ | **GIỮ, hạ ưu tiên xuống làm cùng lúc WS1 thay vì trước** — viết `.importlinter` sau khi biết chính xác import nào cần cấm (làm trước dễ đoán sai contract) |
| WS1 (sửa 5 import ngược) | Có, đã verify circular dependency là thật | **GIỮ NGUYÊN** — đây là fix rẻ nhất, rõ ràng nhất, ít tranh cãi nhất trong toàn bộ roadmap |
| WS2 (Autonomy Control Plane) | **Một phần đúng, một phần overengineered** | **THU NHỎ MẠNH** — xem mục 3, 4. Phần đúng: xoá 369 dòng code chết. Phần sai: xây "Engine" mới bao bọc 2 module đã tách sẵn |
| WS3 (Operational Memory) | Một phần đúng | **THU NHỎ** — bỏ archive-mọi-trace, chỉ archive trace đã promote (xem mục 6) |
| WS4 (Agent Platform) | Chỉ phần trust model đúng lúc | **THU NHỎ MẠNH** — 4/6 hạng mục là giải quyết vấn đề tưởng tượng cho fleet 3 VM (xem mục 8) |
| WS5 (god-object refactor) | Có, xác nhận lại: `omni_worker.py` vẫn 1420 dòng, đã gây bug thật | **GIỮ NGUYÊN**, không tranh cãi |
| WS6 (Execution/Knowledge plane split) | Đúng hướng nhưng thiếu risk quan trọng | **GIỮ, thêm yêu cầu bắt buộc**: idempotency mutation + rollback path qua WS5 registry (mục 9) |
| WS7 (System Twin → blast-radius) | Đúng insight, **SAI polarity thiết kế** | **THIẾT KẾ LẠI HOÀN TOÀN** — xem mục 5. Twin phải làm hệ thống THẬN TRỌNG HƠN, không phải thay thế nguồn an toàn đã kiểm chứng |
| WS8 (Change Intelligence "bounded context") | Insight đúng (thiếu thật), tên gọi sai | **GIÁNG CẤP từ "bounded context" xuống "1 bảng + 1 module truy vấn"** — xem mục 7 |
| WS9 (Remote Agent SDK canary) | Có, rủi ro cao nhất đã đúng | **GIỮ, nhưng phụ thuộc phần đã bị cắt của WS4** — cần xác nhận lại phụ thuộc thật (chỉ cần Ed25519 signing xong, KHÔNG cần chờ registry/lifecycle/canary-automation bị cắt) |
| WS10 (dọn manifest chết) | Có | **GIỮ, MỞ RỘNG phạm vi** thêm việc dọn/vô hiệu hoá `teardown-omni-postgres` (phát hiện mục 0) — đây giờ là việc cấp bách hơn cả dọn manifest K8s |

---

## 2. Overengineering — đi qua từng "component" bị nghi ngờ

| Component | Là bounded context thật hay tách sớm? | Phán quyết |
|---|---|---|
| **Autonomy Control Plane** | Tách sớm — chưa có bằng chứng production nào cho thấy 6-7 điểm check rải rác đã gây lỗi. Bằng chứng NGƯỢC LẠI mạnh hơn: incident có thật (drift 2026-07-02) chính là do MỘT giá trị override bị quên — không phải do logic rải rác | **XOÁ khỏi thiết kế dưới dạng "Engine" mới.** Giữ lại đúng 1 việc: xoá code chết + 1 CRAT event tổng hợp lý do (xem mục 3) |
| **Governance Engine** | Không — đã tồn tại dưới tên `mutate_governance.py`, không cần class wrapper mới | **KHÔNG TẠO MỚI** |
| **Autonomy Engine** | Không — đã tồn tại dưới tên `tier_gate.py`, đã có test 352 dòng riêng | **KHÔNG TẠO MỚI** |
| **Operational Memory (Archival Evidence Store)** | Nửa vời — ý tưởng đúng (đóng gap SOX/PCI thật), nhưng thiết kế "archive mọi trace lúc ghi" là tưởng tượng ra tải trọng không cần thiết | **GIỮ Ý TƯỞNG, ĐỔI CHIẾN LƯỢC** sang lazy + chỉ archive trace đã promote (mục 6) |
| **Change Intelligence (bounded context)** | KHÔNG — đây là use case "thêm 1 bảng Postgres + query theo index", tự nhận "bounded context" là kiến trúc du hành vũ trụ cho 1 tính năng có thể viết trong `services/analyst/` hiện có | **GIÁNG CẤP xuống read-model nội bộ, KHÔNG phải context/service mới** (mục 7) |
| **Capability Registry** (thay `omni_worker.py`) | CÓ — đây là bounded-context hợp lệ duy nhất trong toàn bộ danh sách, vì nó giải quyết đúng 1 bug đã xảy ra thật (god-object gây crash-loop production) | **GIỮ, đây là phần WS5 hợp lý nhất trong roadmap** |
| **System Twin "promotion"** | Cách đóng khung SAI — Twin đã là production code (95 test, đã cắm dây vào advisory), không cần "promote" từ demo. Vấn đề thật hẹp hơn: chỉ 1 hàm (`blast_radius()`) chưa được gọi | **KHÔNG "promote" cả subsystem — chỉ nối đúng 1 hàm, với polarity khác hẳn thiết kế cũ** (mục 5) |
| **Agent Registry (redesign)** | Chưa — 3 VM lab không tạo đủ áp lực vận hành để chứng minh cần Postgres registry mới. TTL Redis=120s là vấn đề CÓ THẬT nhưng cách rẻ nhất để sửa là tăng TTL, không phải đổi kiến trúc lưu trữ | **HOÃN — giải pháp rẻ trước (tăng TTL), Postgres registry chỉ làm nếu tăng TTL không đủ** (mục 8) |

---

## 3. Decision Flow — `AutonomyControlPlane.decide()` có nên tồn tại?

**Câu trả lời: KHÔNG dưới dạng đề xuất ban đầu (1 hàm hợp nhất thay thế 6-7 điểm check).**

Bằng chứng lịch sử trực tiếp bác bỏ ý tưởng "gộp lại cho gọn": memory `project_drift_correction_2026_07_02` ghi nhận incident thật — `OMNI_AUTO_EXECUTE_ENABLED` bị bỏ quên `=true` trên **1** Deployment env, và hệ thống vẫn được cứu vì **nhiều lớp check độc lập khác** (namespace allowlist, tier gate, blast-radius) đã giới hạn thiệt hại thay vì để 1 điểm hỏng lan ra toàn bộ. 7 điểm check `OMNI_AUTO_EXECUTE_ENABLED` không phải là "phân mảnh cần dọn" — đó là **defense-in-depth đã cứu hệ thống ít nhất 1 lần có bằng chứng thật**.

Nếu gộp 6-7 điểm này thành 1 hàm `decide()` trung tâm:
- **God Object thật**: mọi lỗi trong 1 hàm này giờ ảnh hưởng TOÀN BỘ đường mutate, thay vì chỉ 1 lớp trong chuỗi phòng thủ.
- **Giảm resilience, không tăng**: hiện tại nếu 1 layer có bug (ví dụ tier_gate tính sai), layer khác (blast-radius, governance namespace check) vẫn có cơ hội chặn. Gộp vào 1 hàm nghĩa là 1 bug logic = mất TOÀN BỘ phòng thủ cùng lúc.
- **Không phải bottleneck hiệu năng** (đã verify: `replicas: 1` ở cả `omni-fullstack` lẫn `omni-gateway`, gọi hàm trong cùng process — không phải network call) — nên lý do "gộp cho nhanh" không áp dụng, chỉ còn lý do "gộp cho dễ đọc" — không đủ mạnh để đánh đổi resilience.

**Thiết kế thay thế: Decision Transparency Layer, không phải Decision Consolidation Engine.**
Giữ nguyên toàn bộ 7 điểm check phân tán như hiện có (đây là tính năng, không phải smell). Chỉ thêm **1 việc**: tại điểm cuối cùng của chuỗi (`kafka_actions_consumer._handle_execute_mutate`, nơi đã tuần tự gọi qua tất cả các lớp), ghi 1 CRAT event `DECISION_RENDERED` tổng hợp **kết quả** (không phải logic) của từng lớp đã chạy — `{killswitch: bool, governance: ALLOW/DENY, tier_gate: ALLOW/HITL/SUGGEST, blast_radius: pass/block, final: ...}`. Đây là logging/observability, không phải kiến trúc mới — chi phí gần bằng 0, rủi ro gần bằng 0, đạt đúng mục tiêu "trả lời tại sao Omni quyết định X" mà không đụng vào bất kỳ logic quyết định nào đang chạy đúng.

---

## 4. Governance vs Autonomy — có cần 2 bounded context?

Trả lời trực tiếp 4 câu hỏi:

- **"Governance có thể chỉ là 1 policy provider bên trong Autonomy?"** — Không cần đặt câu hỏi này vì thực tế NGƯỢC LẠI đã đúng từ trước: chúng **đã là 2 module riêng** (`mutate_governance.py` 124 dòng, `tier_gate.py` 206 dòng), gọi tuần tự, không lẫn logic. Không có gì để "tách" — việc tách đã xảy ra tự nhiên qua thời gian, không cần dự án mới.
- **"Autonomy có thể tiêu thụ policy thay vì trở thành service riêng?"** — Đã đúng vậy: `tier_gate.py` KHÔNG chứa logic permission, nó nhận risk_class/tier làm input, không tự quyết định "được phép hay không". Không có service nào ở đây, cả 2 đều là Python module thuần, cùng process.
- **"2 context có tăng chi phí bảo trì không?"** — CÓ, nếu biến chúng thành 2 "Engine" class với API riêng, versioning riêng, docs riêng — điều này KHÔNG cần thiết khi bản chất chỉ là 2 hàm Python thuần được gọi tuần tự trong cùng 1 file caller.
- **"Sự tách biệt có được biện minh bởi domain evolution, hay chỉ là tổ chức code?"** — Chỉ là tổ chức code, và **đã đủ tốt ở hiện tại**. Domain chưa hề "evolve" theo hướng cần 2 team/2 lifecycle độc lập cho Governance và Autonomy — cả 2 thay đổi cùng nhịp với nhau (mọi lần đổi risk-class matrix đều kéo theo xem lại namespace allowlist).

**Kết luận:** bác bỏ hoàn toàn việc tạo 2 "Engine" mới. Việc DUY NHẤT cần làm ở WS2: xoá `pkg/autonomy/gate.py`+`policy.py` (369 dòng code chết, 1 call site cần sửa ở `gateway/routes/autonomy.py`), và thêm Decision Transparency Layer (mục 3). Không có bounded context mới nào ở đây.

---

## 5. World Model — Twin có nên thành nguồn blast-radius chính?

**Câu trả lời: KHÔNG theo polarity đã đề xuất ở WS7 (Twin trước, K8s-rule fallback). Đây là lỗi thiết kế nghiêm trọng cho 1 safety gate.**

Trả lời từng câu hỏi:
- **Khi đồ thị cũ (stale)?** — Twin có versioned history (200 revision) nhưng KHÔNG có cơ chế "hết hạn" — 1 revision từ 3 tuần trước vẫn được coi là "đủ dữ liệu" nếu thiết kế WS7 chỉ check "có Fact hay không", không check độ mới thực tế so với ngưỡng an toàn. Hạ tầng khách hàng đổi liên tục (discovery diff xác nhận điều này) — dùng Twin cũ để TÍNH TOÁN AN TOÀN của 1 mutation là rủi ro thật.
- **Lúc onboarding?** — Twin gần như trống ở tenant mới (chỉ có dữ liệu sau khi discovery chạy vài chu kỳ) — nếu logic là "Twin có dữ liệu → dùng nó, tin tưởng nó", 1 tenant mới onboard mà đã có nhu cầu autonomous-execute (tier `auto`) sẽ bị đánh giá blast-radius sai lệch nghiêm trọng vì Twin rỗng ở đúng lúc rủi ro cao nhất.
- **Discovery không đầy đủ?** — `connects_to`/`hosts` chỉ phủ network-level, KHÔNG có `depends_on`/`calls` thật (đã xác nhận Đ14) — nghĩa là ngay cả khi Twin "có dữ liệu", đồ thị phụ thuộc thật (thứ cần cho blast-radius) THIẾU hoàn toàn ở production hiện tại. WS7 thiết kế "dùng Twin nếu đủ dữ liệu" nhưng KHÔNG BAO GIỜ đủ dữ liệu với predicate hiện có — nghĩa là điều kiện fallback gần như luôn đúng, code path chính (Twin-based) gần như không bao giờ chạy trong thực tế cho tới khi collector được mở rộng — greenlighting 1 nhánh code gần như chưa test được trong production thật.
- **Confidence sai?** — Twin's `Fact.confidence` do provenance quyết định (agent tự báo cáo) — không có cơ chế đối chiếu độc lập như z-score/3σ có ở os_host baseline. Một Fact sai (agent bug, hoặc dữ liệu race) có thể tự tin báo "host A không phụ thuộc host B" trong khi thực tế có — nếu Twin THAY THẾ K8s-rule, kết quả là bỏ sót blast-radius thật.
- **K8s có nên luôn là nguồn sự thật?** — **CÓ, cho phần safety-critical.** K8s live API phản ánh trạng thái THẬT ngay tại thời điểm quyết định (không có độ trễ/staleness như Twin), đã có 121 dòng test, đã chạy production không sự cố.

**Thiết kế đúng: Twin CHỈ ĐƯỢC PHÉP MỞ RỘNG (widen) blast-radius, không bao giờ được THU HẸP (narrow) nó.**
```
final_blast_radius = union(
    k8s_live_rule_result,       # luôn chạy, luôn là sàn tối thiểu, không đổi
    twin_based_result if twin_confidence_and_freshness_ok else EMPTY,
)
```
Nếu Twin gợi ý thêm resource bị ảnh hưởng (ví dụ phát hiện `depends_on` mà K8s OwnerReference không thấy vì đó là dependency tầng ứng dụng) → union làm blast-radius RỘNG HƠN → an toàn hơn, đúng hướng fail-safe. Twin KHÔNG BAO GIỜ được dùng để nói "K8s rule chặn nhưng Twin nói an toàn nên cho qua" — đảo polarity so với thiết kế WS7 ban đầu (Twin trước, K8s fallback) nhưng đúng nguyên lý an toàn hệ thống (fail-closed, không fail-open theo dữ liệu ít được kiểm chứng hơn).

---

## 6. Operational Memory — replay có cần Archival Evidence Store mới?

- **Replay có nên nằm trong CRAT không?** — Không, và thiết kế WS3 đã đúng ở điểm này (không nhét vào CRAT schema) — giữ nguyên.
- **Archival Evidence Store có trùng lặp storage đã có không?** — Không hẳn trùng lặp (CRAT là tamper-evident hash-chain, archive là nội dung thô — 2 mục đích khác nhau, tách bảng là hợp lý), NHƯNG chiến lược "ghi archive cho MỌI trace tại thời điểm ghi CRAT" là quá tải không cần thiết — tuyệt đại đa số trace chỉ là `METRIC_SAMPLE` OBSERVED bình thường, không bao giờ được ai tra lại.
- **Cần hệ storage khác (object storage) không?** — KHÔNG, evidence mỗi trace nhỏ (vài KB), Postgres blob/jsonb đủ dùng — thêm S3/object-storage là burden vận hành không cần thiết ở quy mô hiện tại (thêm 1 dependency, 1 credential, 1 lifecycle policy phải quản lý cho lợi ích chưa chứng minh).
- **Replay có thể sinh lazy thay vì archive sẵn không?** — **CÓ, và nên làm vậy cho phần lớn trường hợp.** Đa số nhu cầu "xem lại quyết định" xảy ra trong vài giờ (còn trong TTL của RAG-session/diag-session) — endpoint `/trace/{id}/replay` LAZY (đọc trực tiếp từ nguồn còn sống) đã đủ cho >90% nhu cầu vận hành thật. Chỉ nhu cầu compliance dài hạn (SOX/PCI, có thể hỏi lại sau nhiều tháng) mới cần dữ liệu bền — và nhu cầu đó chỉ áp dụng cho trace ĐàPROMOTE thành ANOMALY/HITL/EXECUTE (tập nhỏ, đã lọc sẵn qua `_promote_to_anomaly`), không phải mọi trace.

**Thiết kế lại WS3:** bỏ "archive tại thời điểm ghi cho mọi CRAT event" → chỉ archive khi 1 trace promote thành ANOMALY (đúng điểm dedup 600s đã có sẵn trong `knowledge_pipeline`, tận dụng luôn, không thêm hook mới) — cắt write-amplification xuống đúng bằng tỷ lệ trace thật sự quan trọng (theo dữ liệu Đ9-Đ12, phần lớn METRIC_SAMPLE không bao giờ promote). Learning write-back (`LEARNING_RECORDED`) và `/compliance/export?trace_id=` giữ nguyên — 2 việc này rẻ, đúng, không có gì để cắt.

---

## 7. Change Intelligence — có xứng đáng là service riêng?

**Không.** Trả lời trực tiếp:
- **Có thể là 1 projection có index của CRAT không?** — Đúng một phần: CRAT (`CONFIG_CHANGED`) là 1 trong 3 nguồn, nhưng discovery-diff không đi qua CRAT — nên "chỉ project CRAT" là không đủ, cần hợp nhất từ Kafka event trực tiếp (discovery diff) + CRAT — nhưng vẫn KHÔNG cần 1 service mới để làm việc này.
- **Có đáng 1 service riêng?** — Không. Đây là 1 bảng Postgres (`change_event`, index `(resource_id, ts)`) + 1 hàm `get_changes_before()`. Postgres đã là dependency có sẵn (14+ migration `omni_admin`), không cần hạ tầng mới, không cần deployment mới, không cần Kafka consumer mới ngoài việc subscribe thêm 1 topic đã tồn tại vào 1 consumer đã chạy (`kafka_evidence_loop` hoặc tương đương).
- **Postgres projection có đủ không?** — Đủ, hoàn toàn.
- **Đang phát minh 1 subsystem thay vì 1 read-model?** — Đúng, đó chính xác là lỗi của bản thiết kế trước. `OMNI_V2_ARCHITECTURE_REDESIGN.md` Phase 2 liệt kê "Change Intelligence" như 1 bounded context ngang hàng "Reasoning"/"Execution" — sai tầm quan trọng. Đây là 1 feature nhỏ nằm gọn trong `services/analyst/` (nơi context evidence được build cho LLM prompt) — không phải 1 context riêng có API/data-ownership/lifecycle độc lập.

**Sửa:** hạ toàn bộ "Change Intelligence" trong Phase 2/3 của redesign doc xuống thành 1 module (`services/analyst/change_context.py` hoặc tương tự) thay vì 1 bounded context trong sơ đồ kiến trúc.

---

## 8. Agent Platform — Ed25519 hay Sigstore/SPIFFE/OCI?

- **Ed25519 signing (đề xuất WS4)**: **GIỮ, đây là phần đúng duy nhất của WS4.** Lý do bác bỏ 3 phương án khác:
  - **Sigstore**: cần Fulcio (CA) + Rekor (transparency log) — hoặc dùng instance công khai (nghĩa là tiến trình cập nhật agent trên VM khách hàng phải gọi ra Internet tới hạ tầng bên thứ ba để verify — attack surface mới, dependency mới, không phù hợp môi trường có thể air-gapped/on-prem của khách SRE), hoặc tự host Fulcio/Rekor (vận hành nặng hơn nhiều so với lợi ích ở quy mô hiện tại).
  - **SPIFFE/SPIRE**: giải quyết workload identity cho service mesh quy mô lớn (hàng trăm+ workload) — Omni hiện có 3 VM lab, KHÔNG phải bài toán fleet lớn. Triển khai SPIRE server + agent attestation là đầu tư hạ tầng không tương xứng với quy mô bài toán thật.
  - **OCI artifact signing (cosign)**: chỉ hợp lý NẾU bundle đã là OCI artifact — thực tế bundle là tar.gz qua `scripts/omni-agent-bundle.sh` (không phải container image). Có 1 biến thể containerized khác (`ghcr.io/omni/omni-agent:latest`, DaemonSet trong k8s/) — nếu đó là hướng phát triển chính trong tương lai, cosign hợp lý CHO NHÁNH ĐÓ, nhưng không giải quyết được path tar.gz hiện tại đang dùng cho VM khách hàng thật.
  - **Kết luận:** Ed25519 tái dùng `services/audit_ledger/signer.py` đúng là lựa chọn tối ưu ở quy mô hiện tại — không thêm hạ tầng, không thêm dependency mạng ngoài, đã có pattern kiểm chứng trong chính repo.
- **Registry redesign (Postgres hoá metadata)**: **HOÃN.** TTL Redis=120s là vấn đề thật nhưng cách rẻ nhất là **tăng TTL** (ví dụ 24h) trước — đây là đổi 1 hằng số, deploy trong phút, không cần schema/migration mới. Chỉ làm Postgres registry nếu sau khi tăng TTL vẫn còn nhu cầu vận hành cụ thể chưa được đáp ứng (ví dụ cần lịch sử version qua nhiều tháng — chưa có bằng chứng nhu cầu này tồn tại).
- **Lifecycle states (`degraded`/`deprecated`)**: **HOÃN.** Chưa có bằng chứng vận hành nào cho thấy `active`/`revoked` không đủ — YAGNI thật sự ở đây, không phải overengineering nhẹ mà là xây trước khi có nhu cầu xác nhận.
- **Canary rollout tự động**: **HOÃN, GIÁNG CẤP xuống quy trình thủ công.** Fleet 3 VM — canary "tự động so sánh health" là giải bài toán của fleet hàng trăm agent. Ở quy mô 3, operator gọi API update cho 1 agent, tự quan sát dashboard, rồi gọi cho 2 agent còn lại — quy trình thủ công NÀY ĐÃ ĐỦ, không cần code thêm.

**WS4 sau khi cắt: chỉ còn Ed25519 signing + version-compat reject gate (rẻ, giá trị cao, không phụ thuộc quy mô fleet).** Cắt khoảng 60-70% khối lượng công việc ban đầu.

---

## 9. Missing Risks (bổ sung, không có trong 3 tài liệu trước)

1. **`teardown-omni-postgres` là landmine sống** (mục 0) — phải trung hoà (xoá script, hoặc sửa lại tên deployment + thêm guard kiểm tra `omni_admin` có bảng nào ngoài RAG trước khi cho chạy) TRƯỚC KHI WS3/WS4/WS8 thêm bảng mới vào cùng Postgres — thêm dữ liệu load-bearing vào 1 instance có sẵn 1 con đường xoá-nhầm không được canh gác đúng là tăng blast-radius của chính landmine đó.
2. **Không có chiến lược versioning cho Kafka event schema** ngoài field `schema_version` mới thêm cho riêng agent envelope (WS4, đã hoãn). `omni-actions`/`omni-action-feedback`/`omni-diagnostic-evidence` không có story tương tự khi WS2 (event `DECISION_RENDERED`)/WS8 (`recent_changes`) thêm field mới — cần xác nhận consumer hiện tại (KPI, dashboard) dùng Pydantic `extra="ignore"` hay `extra="forbid"` trước khi thêm field bất kỳ.
3. **WS6 rollback phụ thuộc ẩn vào WS5**: nếu Capability Registry (WS5) xoá hẳn khả năng chạy execution loop trong `omni-fullstack` sau khi tách sang `omni-execution` (WS6), rollback WS6 (scale `omni-execution` về 0) sẽ KHÔNG có tác dụng — `omni-fullstack` không còn biết cách tự chạy execution nữa. Yêu cầu bắt buộc: Capability Registry phải giữ khả năng "execution capability chạy trong bất kỳ deployment nào" tới khi WS6 được chứng minh ổn định qua ít nhất 1 chu kỳ observation dài (giống mẫu 21 phút đã dùng cho fix onboarding).
4. **Idempotency của Execution Plane mutation chưa được đặc tả** — không phải vấn đề riêng của WS6, đã tồn tại từ v1, nhưng WS6 làm lộ rõ nó hơn (network hop qua Kafka giữa 2 Deployment thay vì gọi hàm cùng process tăng khả năng retry/duplicate message). Cần xác nhận: chạy lại cùng 1 `decision`/`command_id` 2 lần có an toàn không (hiện dựa vào rate-limit, chưa chắc là idempotency thật).
5. **Prometheus gauge mới (nếu WS2/WS8 thêm) lặp lại đúng lớp bug đã xảy ra 2 lần** (`omni_kafka_consumer_lag` kẹt cao giả — Đ7; `omni_kpi_advisory_acceptance_rate` kẹt thấp giả — Đ1) — bất kỳ gauge mới nào PHẢI thiết kế decay/reset rõ ràng ngay từ đầu, không phải "thêm rồi sửa sau khi phát hiện lại".
6. **LLM context budget cho `recent_changes` (WS8, đã giáng cấp) chưa qua `llm_context_budget.build_llm_options`** — nếu làm, phải đi qua cơ chế cap ngân sách đã có (`OMNI_LLM_NUM_CTX`), không tự ý thêm field vào prompt mà không tính vào budget hiện có (System Twin block đã chiếm 800 ký tự, cộng RAG snippet, cộng `recent_changes` mới — rủi ro tràn ngân sách cộng dồn).
7. **WS9 (chuẩn hoá 5 collector) không có story dual-format rõ ràng** — chỉ ghi "canary" nhưng canary giải quyết "rollout dần", không giải quyết "gateway phải hiểu được CẢ payload cũ (agent tự phán FAILED/PASSED) LẪN payload mới (OBSERVED-only) đồng thời trong lúc rollout dở dang" — cần đặc tả rõ nhánh xử lý kép ở `knowledge_pipeline`/`agent_webhook` trước khi bắt đầu, không phải phát hiện giữa chừng.
8. **Không có chiến lược backup/restore cho `omni-postgres`** — vấn đề có từ v1 (14+ migration đã load-bearing, StatefulSet đơn, không thấy CronJob backup nào trong `k8s/`), nhưng v2 làm nó XẤU HƠN bằng cách thêm 3 bảng nữa (`change_event`, `trace_evidence_archive`, `agent_metadata` — dù đã hoãn `agent_metadata` ở mục 8) vào cùng 1 điểm lỗi đơn (single point of failure) chưa có backup. Nên coi đây là điều kiện tiên quyết (must-fix) trước WS3/WS8, không phải việc làm sau.

---

## 10. Simplification Challenge — cắt tối thiểu 20%

| Hạng mục | Cắt gì | Ước lượng giảm |
|---|---|---|
| WS2 | Bỏ "AutonomyControlPlane + GovernanceEngine + AutonomyEngine" (module/class mới) → chỉ còn "xoá 369 dòng code chết + 1 CRAT event tổng hợp" | **~70%** khối lượng WS2 ban đầu |
| WS4 | Bỏ Registry Postgres, Lifecycle states, Canary automation → chỉ còn Ed25519 signing + version-compat gate | **~60-70%** khối lượng WS4 ban đầu |
| WS8 | Bỏ khung "bounded context" → còn "1 bảng + 1 module truy vấn trong `services/analyst/`" | **~50%** khối lượng WS8 ban đầu (giá trị giữ nguyên gần như 100%) |
| WS3 | Bỏ archive-mọi-trace-lúc-ghi → chỉ archive trace đã promote ANOMALY | **~40%** write-amplification, giữ nguyên giá trị compliance |
| Phase 2/3 của `OMNI_V2_ARCHITECTURE_REDESIGN.md` | Xoá "Change Intelligence" và "Governance"+"Autonomy" (2 context riêng) khỏi sơ đồ 11 bounded context → còn 9 context thật | Giảm số lượng "context" cần maintain lâu dài, không chỉ giảm code |

**Tổng thể: cắt được nhiều hơn 20% yêu cầu** — phần lớn cắt nằm ở việc từ chối tạo class/module/context MỚI cho những gì 2 module Python đã làm đúng từ trước, và từ chối giải quyết bài toán fleet-lớn cho 1 fleet 3 VM.

---

# Final Verdict

## Executive Summary

**READY WITH CHANGES.**

Chẩn đoán gốc ở `OMNI_V2_ARCHITECTURE_REDESIGN.md` (god-object `omni_worker.py`, circular dependency `pkg/anomaly/rag→workers`) là ĐÚNG và đã verify lại bằng code thật — WS0/WS1/WS5/WS10 giữ nguyên, không tranh cãi. Nhưng phần lớn các "component mới" được đề xuất ở Đ14 (Autonomy Control Plane, Governance/Autonomy Engine, Change Intelligence bounded context, Agent Registry đầy đủ) là **overengineering thật** — giải quyết vấn đề tưởng tượng hoặc vấn đề đã được giải quyết đủ tốt ở mức module. World Model (`aoip`) bị đánh giá sai độ trưởng thành (không phải demo — production code có 95 test), và thiết kế polarity cho blast-radius (Twin trước, K8s fallback) là **sai hướng an toàn** cho 1 safety gate.

## Critical Architecture Problems

1. **Polarity sai ở WS7**: dùng World Model chưa đủ trưởng thành (thiếu `depends_on`/`calls` predicate thật) làm nguồn CHÍNH cho blast-radius, để K8s-rule đã kiểm chứng làm fallback — đảo ngược nguyên lý fail-safe. Phải sửa thành union-mở-rộng trước khi code bất kỳ dòng nào của WS7.
2. **`teardown-omni-postgres` landmine sống** — không thuộc v2 nhưng phải trung hoà trước khi WS3/WS4/WS8 thêm bảng vào cùng Postgres.
3. **Thiếu backup/restore cho `omni-postgres`** — điều kiện tiên quyết trước khi thêm bất kỳ bảng load-bearing mới nào (WS3, WS8).

## Overengineered Components

- Autonomy Control Plane / Governance Engine / Autonomy Engine (mục 2, 3, 4) — xoá khỏi thiết kế, giữ 2 module hiện có + 1 event log tổng hợp.
- Change Intelligence "bounded context" (mục 7) — giáng cấp xuống read-model nội bộ trong `services/analyst/`.
- Agent Registry/Lifecycle/Canary automation (mục 8) — hoãn tới khi fleet-scale thật sự đòi hỏi, chỉ giữ Ed25519 signing.
- Archive-mọi-trace trong Operational Memory (mục 6) — chỉ archive trace đã promote.

## Missing Production Concerns

Xem đầy đủ mục 9 — trọng tâm nhất: landmine Postgres teardown, thiếu backup/restore, thiếu Kafka schema-versioning strategy, thiếu đặc tả idempotency cho Execution Plane, thiếu dual-format handling cho WS9 rollout.

## Better Architecture

Thay vì 11 bounded context (bản Đ13) hoặc thêm 2 "Engine" mới (Đ14), kiến trúc v2 nên có **9 bounded context thật** (bỏ Change Intelligence và gộp Governance+Autonomy trở lại thành 1 context "Mutation Safety" chứa cả `mutate_governance.py`+`tier_gate.py`+`blast_radius.py` như hiện tại, chỉ thêm observability, không thêm abstraction), giữ nguyên Control/Knowledge/Execution/Agent/Data plane (Phase 6 của redesign doc — phần này không bị red team bác bỏ, không tìm thấy vấn đề). Đổi 1 nguyên lý cốt lõi: **World Model chỉ được phép làm hệ thống thận trọng hơn, không bao giờ ít thận trọng hơn** — áp dụng cho mọi lần dùng dữ liệu suy luận (Twin, RAG, LLM) trong đường mutate.

## Revised Workstream Order

```
Sóng 1: WS1 (sửa import ngược) → WS0 (viết .importlinter SAU khi biết chính xác contract)
Sóng 2 (must-fix, không thuộc v2 nhưng chặn đường):
   - Trung hoà teardown-omni-postgres landmine
   - Thiết kế backup/restore cho omni-postgres (trước khi thêm bảng mới)
Sóng 3: WS2 (thu nhỏ — xoá code chết + Decision Transparency Layer, KHÔNG xây Engine mới)
Sóng 4 (song song): WS3 (thu nhỏ — lazy replay + archive-chỉ-trace-promote), WS4 (thu nhỏ —
   chỉ Ed25519 signing + version gate), WS10 (dọn manifest chết + landmine ở trên)
Sóng 5: WS5 (god-object refactor) → WS6 (plane split, kèm yêu cầu idempotency + rollback-safe registry)
Sóng 6: WS7 (Twin→blast-radius, polarity union-mở-rộng) + WS8 (thu nhỏ — 1 bảng, không phải context)
Sóng 7: WS9 (canary rollout collector, sau khi WS4 xong phần Ed25519 — KHÔNG cần chờ phần đã hoãn)
```

## Must Fix Before Coding

1. Trung hoà `teardown-omni-postgres` (xoá hoặc sửa tên deployment + thêm guard) — **trước WS3/WS4/WS8**.
2. Xác nhận chiến lược backup/restore cho `omni-postgres` — **trước khi thêm bảng mới bất kỳ**.
3. Đảo polarity thiết kế WS7 (union-mở-rộng, không phải Twin-trước-K8s-fallback) — **trước khi viết 1 dòng code WS7**.
4. Xoá đề xuất "Change Intelligence bounded context" khỏi Phase 2/3 của `OMNI_V2_ARCHITECTURE_REDESIGN.md`, thay bằng read-model nội bộ — **trước khi WS8 bắt đầu**.
5. Xoá đề xuất "AutonomyControlPlane/GovernanceEngine/AutonomyEngine" khỏi WS2, thay bằng "xoá code chết + Decision Transparency Layer" — **trước khi WS2 bắt đầu**.
6. Đặc tả idempotency cho Execution Plane mutation — **trước WS6**.
7. Đặc tả dual-format handling (agent cũ vs mới) cho WS9 — **trước WS9**, không phải phát hiện giữa chừng khi đã canary trên VM khách hàng thật.
