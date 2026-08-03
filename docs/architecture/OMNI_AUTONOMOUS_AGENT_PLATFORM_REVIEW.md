# Omni — Autonomous Agent Platform Review (bổ sung trước khi chốt v2)

**Vai trò:** Chief Software Architect · **Ngày:** 2026-08-03 · **Trạng thái:** Review bổ sung, feed vào `OMNI_V2_ARCHITECTURE_REDESIGN.md` — chưa code
**Khung nhìn:** không đánh giá Omni như phần mềm giám sát có AI, mà như **một Autonomous SRE Operating System** — hệ điều hành tự vận hành, có quyết định, có trí nhớ, có agent dưới quyền.
**Phương pháp:** 5 khảo sát Explore song song đọc code thật (không suy đoán, không tin lại tài liệu/memory cũ) — chi tiết trích dẫn hàm/file cụ thể ở từng mục.

---

## 1. Autonomy Architecture

### 1.1 Hiện trạng thật (5 quyết định "khi nào")

| Quyết định | Cơ chế thật | File/hàm |
|---|---|---|
| **Khi nào Observe** | Chủ yếu pull định kỳ (agent tự poll). **Có on-demand thật**: Omni có thể chủ động nhét `diagnostic_probe` read-only vào hàng đợi lệnh, agent poll và chạy | `gateway/routes/agent_commands.py::enqueue`, `omni:agent:cmd:{id}` |
| **Khi nào Diagnose** | `ConfidenceLevel` của host quyết định trọng số z-score vs ngưỡng tĩnh; lệch → promote ANOMALY (dedup 600s) → mới chạy RAG+LLM | `knowledge_pipeline._decide_metric_deviation/_promote_to_anomaly` |
| **Khi nào Ask Human** | `tier_gate.evaluate_tier_gate(tier, risk_class)` + `blast_radius.assess_blast_radius()` (hard-block độc lập, không phụ thuộc tier) | `pkg/autonomy/tier_gate.py`, `pkg/executor/blast_radius.py` |
| **Khi nào Execute** | Chuỗi 3 lớp: Producer (kill-switch→CRAT→Kafka) → Consumer (shadow-mode→kill-switch lần 2→tier gate→rate-limit) → Executor (nsenter-lock→governance→blast-radius→snapshot→allowlist) | `evidence_mutate_emit.py`, `kafka_actions_consumer.py`, `autonomous_execute.py` |
| **Khi nào Learn** | Ngay khi nhận `omni-action-feedback` (gắn liền execute), KHÁC với cập nhật confidence baseline (chạy theo chu kỳ đếm mẫu, không gắn execute) | `autonomous_feedback_loop.py`, `knowledge_pipeline._handle_metric_sample` |

**Đây là phát hiện quan trọng nhất của toàn bộ review này:** 5 quyết định trên **không nằm ở 1 nơi** — chúng rải qua **6 file khác nhau** (`evidence_mutate_emit`, `kafka_actions_consumer`, `autonomous_execute`, `tier_gate`, `blast_radius`, `auto_recovery_bridge`), mỗi nơi tự check lại một phần điều kiện (ví dụ kill-switch `OMNI_AUTO_EXECUTE_ENABLED` được check **2 lần** ở 2 file khác nhau — đúng defense-in-depth, nhưng không ai tổng hợp "tại sao Omni quyết định X" thành 1 câu trả lời duy nhất; phải đọc lại cả chuỗi 3 tầng mới tái tạo được lý do.

### 1.2 Phát hiện gây ngạc nhiên nhất: có 2 hệ "autonomy" song song, 1 hệ đã CHẾT

`pkg/autonomy/policy.py` (`AutonomyPolicyStore`, `AutonomyLevel.FULL_AUTO/SUGGEST_ONLY/HITL/ALERT_ONLY`) + `pkg/autonomy/gate.py` (`AutonomyGate.evaluate`) là một **hệ quyết định autonomy đầy đủ, độc lập** — nhưng grep xác nhận **không nơi nào trong `kafka_actions_consumer.py`/`autonomous_execute.py` gọi tới nó**. Nó chỉ được `gateway/routes/autonomy.py` dùng để phục vụ 1 API cấu hình. Hệ thực sự gate mutate trong production là `tier_gate.py` (tier×risk_class matrix). Đây là code sống nhưng lạc lối — một trong hai hệ phải được coi là chính thức, hệ kia cần khai tử hoặc hợp nhất.

### 1.3 Governance vs Autonomy — CHƯA tách, đúng như user nghi ngờ

| | Trả lời câu hỏi | Module thật | Tính chất |
|---|---|---|---|
| **Governance** (permission) | "Có được PHÉP làm việc này không, bất kể tin cậy bao nhiêu?" | `mutate_governance.py` (prod posture, namespace allowlist, tool allowlist) | Tĩnh, thay đổi hiếm, nên cần approval khi đổi |
| **Autonomy** (authority) | "Omni có ĐỦ TIN CẬY để tự làm ngay bây giờ không?" | `tier_gate.py` (tier×risk_class) + `remote_host_baseline.ConfidenceLevel` | Động, học theo thời gian/host/tenant |
| **Blast-radius** | "Nếu làm, ảnh hưởng lan tới đâu?" | `blast_radius.py` | Hard-block độc lập, không thuộc về Governance lẫn Autonomy — nó là input feed vào cả hai |

Hiện tại 2 khái niệm đầu bị gọi tuần tự trong `autonomous_execute.py` như thể chúng là 1 chuỗi check phẳng, không được kiến trúc hoá thành 2 câu hỏi tách biệt có thể audit riêng ("bị chặn vì KHÔNG ĐƯỢC PHÉP" khác hẳn ý nghĩa với "bị chặn vì CHƯA ĐỦ TIN CẬY" — vận hành viên cần biết chính xác loại nào để biết phải sửa policy hay phải chờ confidence tăng).

### 1.4 Thiết kế: Autonomy Control Plane

```
                         ┌─────────────────────────────┐
  AnalystAdvisory   ───▶ │   AUTONOMY CONTROL PLANE     │ ───▶ Decision{permission, authority, reason_chain}
  (candidate action)     │                              │
                         │  ┌────────────┐ ┌───────────┐│
                         │  │ Governance │ │ Autonomy  ││
                         │  │  Engine    │ │  Engine   ││
                         │  │ (permission)│ │(authority)││
                         │  └─────┬──────┘ └─────┬─────┘│
                         │        │              │       │
                         │        ▼              ▼       │
                         │   DENY/ALLOW    EXECUTE/       │
                         │   (tĩnh)        SUGGEST/HITL   │
                         │                 (động, theo    │
                         │                  tier+confidence│
                         │                  +blast-radius)│
                         └─────────────────────────────┘
                                     │
                          write_audit_block(DECISION_RENDERED,
                            reason_chain=[...])  ◀── MỚI: 1 event
                                                      duy nhất giải
                                                      thích quyết định
```

- **Governance Engine** = hợp nhất `mutate_governance.py` — trả lời permission thuần, không đọc confidence/tier.
- **Autonomy Engine** = hợp nhất `tier_gate.py` + `ConfidenceLevel` + `blast_radius.py` (blast-radius là 1 input của authority, không phải bước riêng) — trả lời authority thuần, không đọc policy tĩnh.
- **Kết quả:** 1 hàm `AutonomyControlPlane.decide(action, context) -> Decision` — đây là API DUY NHẤT mà Execution Plane được gọi trước khi mutate (thay vì 6 file tự check rải rác như hiện tại). Quyết định luôn kèm `reason_chain` structured (không phải log text) ghi vào CRAT bằng 1 event `DECISION_RENDERED` — trả lời được ngay "bị chặn vì permission hay vì chưa đủ tin cậy" mà không cần đọc code.
- **Khai tử `pkg/autonomy/gate.py`/`policy.py`** (hệ chết) hoặc hợp nhất logic `AutonomyLevel` của nó vào Autonomy Engine nếu có ý định dùng lại — không giữ 2 hệ song song vô thời hạn.

---

## 2. System Intelligence (System Twin / World Model)

### 2.1 Hiện trạng — nền tảng tốt hơn tưởng, nhưng bị bỏ phí đúng ở chỗ quan trọng nhất

`src/aoip/system_model.py` + `system_model_store.py` là một world model **thật, không phải khung sườn rỗng**: `Fact` là triple bitemporal (subject/predicate/object + confidence + provenance + observation_time/verified_time), có đồ thị quan hệ thật (`RELATIONAL_PREDICATES`: runs_on/depends_on/connects_to/calls/owns/serves...), có **lịch sử versioned** (`omni:aoip:system_model_history:{tenant}`, 200 revision gần nhất, CAS optimistic), có API truy vấn thật (`/onboarding/system-twin` trả `revision/summary/entities/edges/unknowns`).

**Nhưng dữ liệu thật sản xuất ra chỉ có 2 loại quan hệ** (`hosts`, `connects_to` — network-level), các predicate "cao cấp" (`depends_on/calls/owns/serves`) chỉ tồn tại trong script demo (`aoip/runner.py`, `aoip/live_recovery.py`), không collector nào ghi chúng thật. Ownership (`owned_by`) chỉ vào được qua con người trả lời Claim UX — không có collector CODEOWNERS/CMDB.

**Phát hiện quan trọng nhất: `blast_radius.py` — bộ não tính lan truyền ảnh hưởng của TOÀN BỘ hệ thống Execution — KHÔNG dùng World Model.** Nó đọc live K8s API (`ClusterReader`, OwnerReferences/label selector, namespace-wide check) hoàn toàn độc lập với `SystemModel`. Hàm `SystemModel.blast_radius()` (BFS ngược trên `dependents_of` — chính xác thứ cần) **có tồn tại** nhưng chỉ được gọi trong 2 script demo, **0 call site trong pipeline production** (`workers/`, `gateway/`, `pkg/executor/` đều không import). Đây là world model xây xong nhưng chưa cắm dây vào nơi cần nó nhất.

### 2.2 Thiết kế v2: World Model là nguồn sự thật chính, K8s live API là fallback có gate

1. **Mở rộng collector để ghi `depends_on`/`calls`/`serves`** — không chỉ `hosts`/`connects_to`. Đây là cải tiến đòn bẩy cao nhất: mỗi domain collector (database/service/network) đã có đủ thông tin thô (connection string, port, service name) để suy ra quan hệ phụ thuộc thật, chỉ chưa được project thành Fact quan hệ.
2. **`blast_radius.py` gọi `SystemModel.blast_radius()` TRƯỚC**, dùng K8s live-rule hiện tại làm **fallback theo confidence** — giống hệt mẫu `ConfidenceLevel` đã áp dụng đúng cho os_host baseline: nếu World Model có đủ dữ liệu (revision mới, confidence cao) cho resource đang xét → dùng đồ thị thật; nếu thiếu → rơi về rule K8s tĩnh như hiện tại (không phải fail-open, là fail-to-known-good).
3. **Ownership feed vào Governance/HITL routing** — hiện `owned_by` tồn tại trong schema nhưng không được dùng để route thông báo HITL. v2: khi 1 quyết định vào nhánh HITL, tra `SystemModel` lấy `owned_by` của resource bị ảnh hưởng, route Telegram/notification tới đúng team thay vì kênh chung.
4. **Giữ nguyên cơ chế bitemporal + history 200 revision** — đây là điểm mạnh thật sự hiếm có, không cần đổi, chỉ cần tăng độ phủ dữ liệu (hiện chỉ có 3 VM lab).

---

## 3. Change Intelligence

### 3.1 Hiện trạng — HOÀN TOÀN THIẾU dưới dạng capability liên kết (xác nhận, không phải suy đoán)

3 mảnh dữ liệu thô đã tồn tại nhưng **không mảnh nào nối với mảnh khác theo trục thời gian+tài nguyên**:
- `remote_agent/discovery.py::diff_discovery()` phát hiện SERVICE_ADDED/REMOVED, PORT_OPENED/CLOSED thật — nhưng luồng xử lý (`knowledge_pipeline._emit_change_detected` → Telegram Approve/Reject → `change_approval_handler.py`) chỉ dùng để cập nhật baseline "bình thường mới", **không ai tra lại nó khi có incident**.
- CRAT có event `CONFIG_CHANGED` nhưng ledger là 1 Redis LIST duy nhất, không index theo resource/thời gian — truy vấn hiện tại (`compliance.py::_fetch_blocks`) là full-scan `lrange` rồi filter Python theo `days`, không lọc theo resource.
- Không có watcher K8s (`resourceVersion`/informer) nào theo dõi rollout Deployment/ConfigMap — Omni chỉ biết về rollout khi CHÍNH NÓ là người thực hiện (qua executor), không biết khi người khác/CI/CD khác deploy.
- Reasoning (`AnalystAdvisory` prompt) **không nhận** context "gần đây có gì thay đổi" — chỉ có RAG lịch sử sự cố tương tự (khác hẳn lịch sử thay đổi hạ tầng).

Đây là khoảng trống thật, không phải thiếu sót nhỏ — một hệ thống tự nhận "Autonomous SRE" mà không trả lời được "cái gì đổi trước khi hỏng" là thiếu đúng năng lực cốt lõi nhất của một SRE giỏi.

### 3.2 Thiết kế v2: Change Intelligence context (mới hoàn toàn)

```
Nguồn thô (đã có, tái dùng)          Change Ledger (MỚI — CQRS read-model)
────────────────────────            ─────────────────────────────────────
discovery diff (SERVICE_*/PORT_*) ─┐
CRAT CONFIG_CHANGED/TIER_CHANGED ──┼──▶ change_event(tenant_id, resource_id,
K8s watch informer (MỚI)         ──┤     change_type, source, ts, detail)
Execution outcome (self-changes) ──┘     Postgres, index (resource_id, ts)
                                              │
                                              ▼
                              get_changes_before(resource, incident_ts, window)
                                              │
                                              ▼
                          inject vào Reasoning context (AnalystAdvisory prompt)
                          field mới: recent_changes[]
```

- **Không xây lại từ đầu** — Change Ledger là 1 **read-model CQRS** dựng từ đúng các event nguồn đã tồn tại (discovery diff, CRAT CONFIG_CHANGED, + K8s watcher cần thêm mới), ghi vào Postgres có index thay vì Redis LIST full-scan. CRAT vẫn giữ vai trò write-once tamper-evident log; Change Ledger là bản chiếu có thể truy vấn nhanh.
- **API bắt buộc**: `get_changes_before(resource_id, incident_ts, window)` — đây là input **bắt buộc** cho bước Diagnose (Phase 4 canonical pipeline ở tài liệu redesign trước) — không phải tính năng tuỳ chọn.
- **Tự tham chiếu**: hành động Execution của chính Omni cũng phải ghi vào Change Ledger (Omni tự rollout để fix sự cố CŨNG là 1 "change" — nếu sự cố mới xảy ra sau đó, câu trả lời "cái gì đổi trước khi hỏng" phải bao gồm cả hành động của chính Omni, không được có điểm mù tự miễn trừ).

---

## 4. Agent Platform

### 4.1 Hiện trạng — xếp mức trưởng thành (dựa code thật)

| Khía cạnh | Mức | Bằng chứng |
|---|---|---|
| Registry (metadata) | Thô sơ | 2 tầng KHÔNG liên kết: Postgres `agent_credential` (chỉ identity) vs Redis `omni:remote_agent:registry:{id}` (version/capabilities, TTL=120s, ephemeral — mất khi agent offline >120s) |
| Capability discovery | Thô sơ | Tự khai báo qua `derive_enabled_collectors()` (đúng triết lý "agent đề xuất") nhưng danh mục 9 collector hardcode import trong `agent.py`, không có plugin loading động |
| Trust model | **Thiếu** | Không mTLS/code-signing. `updater.py` verify sha256 nhưng checksum đến từ chính admin API caller cung cấp — không phải chữ ký đối chiếu public-key cố định. **Supply-chain risk thật**: ai chiếm admin API key + host trong allowlist domain có thể đẩy bundle tuỳ ý |
| Lifecycle | Thô sơ | `agent_credential.status` CHECK constraint chỉ `active|revoked` — không có `deprecated/decommissioned`. "Offline" là ngầm định qua Redis TTL, tách biệt hoàn toàn khỏi vòng đời credential |
| Upgrade strategy | Thô sơ | Push đơn-agent qua queue (`UPDATE_AGENT`), canary *khả thi kỹ thuật* (gọi tuần tự) nhưng không có cơ chế nào enforce so sánh sức khoẻ trước khi lan rộng — 100% thủ công |
| Version compatibility | Thô sơ | `/webhook/agent/versions` chỉ log warning khi `drift_status=drifted`, không reject/gate payload từ agent version cũ |

### 4.2 Thiết kế v2: Agent Platform như 1 sản phẩm, không phải 1 tính năng

1. **Registry hợp nhất**: 1 service Agent Registry, Postgres backing cho cả identity LẪN metadata vận hành (version/capabilities/lifecycle state) — Redis TTL=120s chỉ còn vai trò "liveness heartbeat" tầng trên, không phải nơi duy nhất giữ capabilities.
2. **Trust model — vá lỗ hổng chuỗi cung ứng thật**: tái dùng đúng nguyên lý đã có ở CRAT (`services/audit_ledger/signer.py`, Ed25519) — ký release bundle bằng private key của Omni, agent verify chữ ký đối chiếu public key **pinned cứng trong agent**, không phải checksum do cùng API caller cấp. Đây là ưu tiên bảo mật cao nhất trong toàn bộ Agent Platform vì đây là đường compromise rẻ nhất hiện có (chiếm 1 API key admin → điều khiển mọi VM khách hàng).
3. **Lifecycle đầy đủ trạng thái**: `enrolled → active → degraded → deprecated → decommissioned`, feed vào Autonomy Control Plane — 1 agent `deprecated` phải luôn bị hạ về HITL bất kể tier/confidence, không được tự động execute.
4. **Upgrade có canary thật**: enforce (không chỉ cho phép) — cập nhật 1 agent, giữ ở trạng thái quan sát N phút, so sánh error-rate/health trước khi cho phép lan rộng batch tiếp theo.
5. **Version compatibility có gate thật**: envelope mang version field bắt buộc; gateway từ chối hoặc hạ cấp graceful (không silently accept) khi version dưới mức tối thiểu hỗ trợ — không chỉ log warning như hiện tại.

---

## 5. Operational Memory (Replayable Decision History)

### 5.1 Hiện trạng — nền tảng khóa liên kết TỐT (trace_id nhất quán), nhưng nội dung bị phân mảnh và có TTL ngắn hơn tuổi thọ cần thiết

**Điểm mạnh xác nhận**: `trace_id` là khóa chung THẬT xuyên suốt — CRAT block, `omni:trace:stages:{trace_id}` (13 stage: INGEST/EVIDENCE/RAG/LLM/VERIFY/SCHEMA/KILLSWITCH/CRAT/DISPATCH/HITL/EXECUTOR/FEEDBACK/AUTO_RECOVERY), `omni:brain:session` (RAG, TTL 3600s), `omni:diag:session` (LLM diagnosis chi tiết, TTL 86400s), `omni:crat:llm_reason:*` (raw LLM output, TTL 86400s) — không có hệ ID rời rạc cần join thủ công.

**3 lỗ hổng cụ thể ngăn "replay 1 API call":**
1. CRAT lưu Decision đầy đủ nhưng Observation/Evidence gốc chỉ có **hash + ref con trỏ**, không lưu nội dung — nội dung thật (RAG session, diag session) có TTL hữu hạn (1h/24h), biến mất sau đó dù CRAT (vô thời hạn) vẫn còn hash trỏ tới chỗ trống.
2. `/compliance/export` không nhận `trace_id` — chỉ lọc theo `tenant_id/days`, phải tự tải theo ngày rồi lọc tay.
3. **Learning cơ bản là luồng câm**: `_upsert_action_experience_on_success` không gọi `write_audit_block` — chỉ tăng metric, không để lại vết trong CRAT/trace. Chỉ nhánh hiếm `SOP_PROMOTED` mới ghi ngược `trace_id` gốc. Nghĩa là "Omni đã học được gì từ case này" không thể truy vấn lại từ chính case đó trong đa số trường hợp.

### 5.2 Thiết kế v2: Operational Memory context (đóng 3 lỗ hổng, không xây lại ID scheme)

1. **Archival Evidence Store**: thay vì chỉ hash+ref TTL-out, ghi kèm 1 bản evidence đã nén (không cần raw đầy đủ — evidence chẩn đoán vốn nhỏ) vào store bền (Postgres blob hoặc object storage), khóa `trace_id`, không TTL. Hash trong CRAT vẫn giữ vai trò tamper-evidence; bản thân nội dung nay có nơi lưu dài hạn tương ứng.
2. **Learning phải ghi ngược**: thêm `write_audit_block(LEARNING_RECORDED, trace_id=...)` vào `_upsert_action_experience_on_success` — biến learning từ luồng câm thành 1 stage có vết trong cùng chuỗi trace, đúng yêu cầu "Learning" là 1 trong 7 giai đoạn phải explain được.
3. **1 endpoint tổng hợp**: `GET /trace/{id}/replay` — aggregator đọc từ toàn bộ nguồn hiện có (CRAT decision, trace-stage timeline, diag-session reasoning nếu còn trong TTL, RAG-brain session nếu còn trong TTL, Archival Evidence Store nếu đã hết TTL nguồn gốc, execution outcome, verification result, learning record mới) và trả về đúng cấu trúc 7 giai đoạn Observation→Evidence→Reasoning→Decision→Action→Verification→Learning trong 1 response — không cần di chuyển dữ liệu hiện có, chỉ cần join bằng `trace_id` (đã sẵn nhất quán) cộng 2 phần bổ sung ở mục 1-2.

---

## Tổng hợp — Omni như một Autonomous SRE Operating System

5 khảo sát trên xác nhận một điều nhất quán: **Omni đã có hầu hết các mảnh ghép cần thiết của một Autonomous SRE OS thật sự** (world model bitemporal có thật, tier-based autonomy có thật, CRAT tamper-evident có thật, trace_id nhất quán xuyên hệ thống, per-agent trust có thật) — nhưng **các mảnh ghép mạnh nhất chưa được cắm dây vào đúng chỗ**:

- World Model (`aoip.SystemModel`) đã đúng dữ liệu bitemporal + đồ thị phụ thuộc, nhưng blast-radius (quyết định quan trọng nhất của Autonomy) không dùng nó.
- Autonomy đã có tier+confidence thật, nhưng có 1 hệ song song đã chết (`pkg/autonomy/gate.py`) gây nhiễu, và Governance/Autonomy chưa tách thành 2 câu hỏi kiến trúc riêng biệt.
- CRAT + trace_id đã là xương sống ghi vết tốt, nhưng Evidence gốc bị TTL-out và Learning không ghi ngược — "trí nhớ" bị đứt đúng ở 2 mắt xích ít được để ý nhất.
- Change Intelligence là capability duy nhất **hoàn toàn chưa tồn tại** — đây là khoảng trống thật, cần xây mới (nhưng tái dùng dữ liệu thô đã có sẵn 3/4 nguồn).
- Agent Platform đã đúng triết lý ("agent đề xuất") nhưng mọi khía cạnh vận hành (registry/trust/lifecycle/upgrade/version) đều ở mức thô sơ — đặc biệt trust model có lỗ hổng chuỗi cung ứng thật cần vá trước tiên.

**Kết luận cho kiến trúc v2 cuối cùng:** Omni v2 không cần phát minh lại các capability lõi này — cần thêm **2 bounded context mới** (Change Intelligence, Operational Memory — tách khỏi Learning/Verification thành context độc lập vì nó phục vụ mọi context khác) và **tách Governance & Policy** (đã gộp ở bản redesign trước) thành 2 context riêng — **Governance** (permission tĩnh) và **Autonomy Control Plane** (authority động, hợp nhất tier+confidence+blast-radius) — đúng yêu cầu tách bạch của user. Đồng thời **cắm dây** World Model vào Execution Plane (blast-radius) và Agent Platform vào Governance (lifecycle state ảnh hưởng authority). Đây là các sửa đổi cần đưa vào Phase 2/3/6 của `OMNI_V2_ARCHITECTURE_REDESIGN.md` trước khi coi kiến trúc v2 là bản cuối.
