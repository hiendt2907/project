# Omni — Audit Toàn Diện (2026-06-03)

> Read-only audit. 4 trục: kiến trúc · code Python · bảo mật · test. Mọi finding kèm `file:line` từ grep/read thực tế.
> Phương pháp: 3 sub-agent song song (architect / python-reviewer / security-reviewer) + chạy test suite + quét rác.

---

## TÓM TẮT ĐIỀU HÀNH

Omni là một hệ multi-agent SRE automation **trưởng thành về chức năng** — 4 lane chẩn đoán hoạt động, pipeline Kafka E2E chạy được, CRAT fail-closed + kill-switch được enforce đúng ở path chính, RBAC executor least-privilege có thật, command executor chống injection tốt. **5080/5085 test pass.**

Tuy nhiên có **3 vấn đề HIGH cần xử lý sớm**: (1) `omni-fullstack` — pod DUY NHẤT đang chạy — lại được gắn ClusterRoleBinding cấp quyền ghi RBAC/Secrets cluster-wide, phá vỡ chính nguyên tắc least-privilege; (2) `/metrics` + `/webhook/prometheus` không auth; (3) nợ kỹ thuật god-object nghiêm trọng (`evidence_consumer.py` 2810 dòng, 1 hàm 954 dòng). Cộng thêm test drift làm CI đỏ và rác repo tích tụ.

---

## PHẦN 1 — ĐANG LÀM ĐƯỢC GÌ

| Năng lực | Trạng thái | Bằng chứng |
|---|---|---|
| **4 lane chẩn đoán** (RESOURCE/HARD_FAIL/APP_HTTP/SIEM) | Hoạt động | `resolve_proof_lane()`, `os_state_validator.py` (cov 92.4%), `three_sigma.py`, `log_surge_probe.py` |
| **Pipeline Kafka E2E** | Hoạt động | prober→`omni-diagnostic-evidence`→analyst→`omni-actions`→executor→feedback; import boundary gateway↔worker **0 vi phạm** |
| **CRAT fail-closed** | Enforce đúng (path chính) | `chain_writer.py:177` raise `AuditLedgerError`; `evidence_consumer.py:2243` abort trước Telegram; 8/8 call site catch + abort |
| **Kill-switch auto-execute** | Fail-closed đúng | `settings.py:149` default `False`; `advisory_mode_kill_switch.py:43` block |
| **Executor RBAC least-privilege** | Có thật | `rbac-executor-least-privilege.yaml` dùng RoleBinding namespace-scoped, không cluster-admin |
| **Chống command injection** | Mạnh | `command_executor.py`: `create_subprocess_exec` (no shell), whitelist, block `cat/grep/tail`, block `find -exec/-delete` |
| **Gateway auth** | Đúng cơ bản | `hmac.compare_digest()`, tenant isolation `OMNI_TENANT_APIKEYS`, business routes có `_require_api_key` |
| **Invariant policy** | Implement | `diagnostic_policy.py:245-296` (INV_NO_RESTART/READ_BEFORE_MUTATE/NAMESPACE_ISOLATION) |
| **Smart-SIEM correlation** | Mới wired | `kafka_siem_chains_loop`, `siem_chain_consumer_enabled` (`settings.py:583`) |
| **Test suite** | 5080/5085 pass | 200 test file; gate cov 90% trên core scope (`.coveragerc.gate`) |

---

## PHẦN 2 — ĐANG HỔNG Ở ĐÂU

### 2A. Security gaps

| # | Severity | Vấn đề | File |
|---|---|---|---|
| S1 | **HIGH** | `omni-fullstack` (pod duy nhất chạy) gắn `ClusterRoleBinding` cấp `secrets` + `rbac.../clusterrolebindings` create/patch **cluster-wide** → phá least-privilege; LLM jailbreak/payload độc có thể tạo ClusterRoleBinding tùy ý | `k8s/deployments/omni-fullstack-rbac.yaml:54-70` |
| S2 | **HIGH** | Drift RBAC: `executor-rbac.yaml:61` cấp `secrets get/patch/update` trong khi `rbac-executor-least-privilege.yaml` thì không — 2 file song song, không rõ file nào active | `k8s/deployments/executor-rbac.yaml:61` |
| S3 | **HIGH** | `/metrics` public (lộ rate-limit, circuit breaker, KPI); `/webhook/prometheus` không auth khi `OMNI_GATEWAY_WEBHOOK_SECRET` chưa mount → ai biết URL đều inject fake alert, tốn LLM | `src/gateway/api.py:382,391` |
| S4 | MEDIUM | `OMNI_UNRESTRICTED_TOOL_EXECUTION=true` bypass `MUTATE_TOOL_ALLOWLIST`, không có prod guard | `evidence_consumer.py:1151,1609`; `settings.py:362` |
| S5 | MEDIUM | `remote_agent_pipeline.py:143` emit Telegram **không** `write_audit_block()` trước → lỗ hổng CRAT fail-closed ở path fallback | `src/workers/remote_agent_pipeline.py:143` |
| S6 | MEDIUM | Rate limiting chỉ áp `/webhook/prometheus`; các POST route khác (siem/agents) không có | `src/gateway/api.py` |
| S7 | LOW | Telegram `chat_id=-5174042122` hardcode trong CLAUDE.md (checked-in) | `CLAUDE.md` |

### 2B. Technical debt

| # | Severity | Vấn đề | Bằng chứng |
|---|---|---|---|
| T1 | **HIGH** | God-object: `evidence_consumer.py` 2810 dòng; hàm `reason_from_diagnostic_evidence` **954 dòng** (`:1856`); `handle_action_feedback_envelope` 984 dòng (`autonomous_feedback_loop.py:755`) — vi phạm trần 800 dòng/file, 50 dòng/hàm | đo wc -l |
| T2 | **HIGH** | Vi phạm invariant `build_llm_options`: 5 chỗ inline `getattr(...,"llm_num_ctx",4096)` → LLM context bị cắt còn 4096 thay vì 8192 | `handlers.py:609,837,857,883,1015` |
| T3 | **HIGH** (latent) | Fire-and-forget `asyncio.create_task` không giữ reference → exception bị nuốt, CRAT write có thể skip silently | `remote_agent_pipeline.py:110` |
| T4 | HIGH | 5 chỗ `except Exception: pass` trong critical path evidence (gồm vùng RAG/CRAT) | `evidence_consumer.py:375,458,558,1194,2204` |
| T5 | MEDIUM | `request: Any` / `redis: Any` thay `Request`/`Redis` — vi phạm rule CLAUDE.md | `gateway/routes/kpi.py:34,74`, `siem.py:44`, `agent_push.py:39,46,123`, `tenant_context.py:14` |
| T6 | MEDIUM | `state.recent_messages.append()` mutation shared state 15+ chỗ → logic trùng + race | `handlers.py:1354-1539` |
| T7 | MEDIUM | Anti-pattern `getattr(getattr(ctx,"settings",None),...)` 12+ chỗ | `tool_registry.py:88,114,122`, `proactive_observer.py:350`, `infra_context.py:17,175`, … |
| T8 | LOW | `settings.py` 1637 dòng, 200+ field 1 model — nên tách `LLM/Kafka/Redis/FeatureFlag` settings | |

### 2C. Operational risk

| # | Severity | Vấn đề | Bằng chứng |
|---|---|---|---|
| O1 | HIGH (prod) / MEDIUM (lab) | SPOF: Kafka 1 replica, Redis 1 replica. CRAT phụ thuộc cả hai → một cái down = zero advisory. Cần Redis Sentinel trước prod | `kafka-single.yaml:29`, `redis-standalone.yaml:92` |
| O2 | MEDIUM | YAML split-role (analyst/prober/executor/core) còn `replicas:1` nhưng không dùng (chỉ `omni-fullstack` chạy) → dead complexity, gây nhầm deployment state | `omni-analyst.yaml:14` |
| O3 | MEDIUM | Stale tool `pgvector_health` vẫn trong `data/sop/sop_templates.yaml` + `routing_policy.py` dù đã gỡ khỏi TOOL_REGISTRY | grep `pgvector_health` |
| O4 | LOW | 20 file `chaos_drill_results_*.json` rác ở root (untracked); `dist/` 6 artifact; `.DS_Store`; 39 `__pycache__` | git status |

### 2D. Test gaps

| # | Severity | Vấn đề | Bằng chứng |
|---|---|---|---|
| Q1 | **MEDIUM (CI đỏ)** | 5 test fail: fixture `SimpleNamespace` thiếu `siem_chain_consumer_enabled` (thêm ở commit 529852e nhưng quên update fixture). Production OK (`settings.py:583`) | `test_cov_omni_worker_gaps.py`, `test_track1b_worker_kafka.py` |
| Q2 | LOW | `.coverage` checked-in chỉ còn 1 module (os_state_validator) — stale artifact, không phản ánh suite thật | `.coverage` |

### 2E. Doc lệch code

| Doc nói | Code thực tế | Sev |
|---|---|---|
| CLAUDE.md: "qwen2.5-coder:7b active all roles" | `settings.py:640` default `qwen3.6` | MEDIUM |
| CODEMAPS/architecture.md:77: "`full` (legacy, replicas=0)" | `omni-fullstack` replicas=1 — pod chính, không phải legacy | MEDIUM |
| CLAUDE.md: "split-role scaled to 0" | `omni-analyst.yaml:14 replicas:1` còn nguyên | LOW |

---

## PHẦN 3 — CẦN CẢI THIỆN GÌ (ma trận impact/effort)

```
         HIGH IMPACT
              │
  S1 RBAC ────┤──── T1 tách evidence_consumer
  Q1 fix test │     (god-object)
  S3 auth     │
  T2 llm_ctx  │
──────────────┼────────────────── EFFORT →
  S7 chat_id  │     O1 Redis HA
  O4 rác      │     settings.py split
  O3 stale    │
              │
         LOW IMPACT
   (quick win)        (lớn, lên PLAN)
```

### Top 5 ưu tiên

| # | Hành động | Lý do | Effort |
|---|---|---|---|
| 1 | **Fix RBAC S1+S2**: gỡ `omni-fullstack-executor-mutate-lab` ClusterRoleBinding, apply least-privilege cho `omni-fullstack` SA; hợp nhất 2 file RBAC | Pod đang chạy có quyền ghi RBAC/Secrets cluster-wide — rủi ro leo thang cao nhất | S (1 buổi) |
| 2 | **Fix 5 test (Q1)** + xoá `.coverage` stale | CI đang đỏ; chặn merge | XS (30 phút) |
| 3 | **Fix T2 + T3**: thay 5 inline `llm_num_ctx`→`build_llm_options`; giữ ref fire-and-forget task | LLM context bị cắt nửa; CRAT có thể skip silently | S (nửa buổi) |
| 4 | **Auth S3**: đưa `/metrics` về internal Service, ép `OMNI_GATEWAY_WEBHOOK_SECRET` khi prod | Endpoint public → inject alert tốn LLM | S |
| 5 | **Tách `evidence_consumer.py` (T1)**: `_siem_lane / _resource_lane / _advisory_emit / _telegram_cards` | 2810 dòng impossible to test/reason; nền cho mọi sửa sau | L (2-3 buổi) |

> O1 (Redis/Kafka HA) đưa vào PLAN.md trước production, không urgent ở lab.

---

## PHẦN 4 — DANH SÁCH RÁC ĐỀ XUẤT XOÁ (Pha 2 — CHỜ DUYỆT)

**Nhóm A — Xoá an toàn (artifact/tmp, untracked):**
- `chaos_drill_results_*.json` × 20 ở root → nên gitignore + xoá
- `dist/omni-agent-*.tar.gz` × 5 + `dist/omni-agent-1.0.0/` → build artifact
- `.DS_Store` × 2
- `src/**/__pycache__` × 39 (đã trong .gitignore nhưng tồn tại trên disk)
- `.coverage` (stale, chỉ 1 module)

**Nhóm B — Cần xác nhận trước khi xoá (có thể là chứng cứ):**
- `reports/rag-training-loops/` × 7 file (log + json từ 2026-04-07) — report cũ
- `reports/chaos/*-2026-04-10.md` — UAT report cũ
- `docs/` artifact: `omni_v3_executive_report.pptx`, các `ADVISORY_MODE_*.md` (5 file, có thể đã hợp nhất)

**Nhóm C — Sửa stale, KHÔNG xoá:**
- `data/sop/sop_templates.yaml` + `routing_policy.py`: gỡ ref `pgvector_health`
- CLAUDE.md: sync model name `qwen3.6` vs `qwen2.5-coder`, gỡ chat_id
- `docs/CODEMAPS/architecture.md:77`: sửa nhãn `full (legacy)` → pod chính

**KHÔNG xoá:** `docs/post-mortems/*` (20 file — chứng cứ), `reports/incident-matrix/`, CRAT-related.

---

## PHẦN 5 — TRẠNG THÁI XỬ LÝ (Pha 2 — đã thực thi 2026-06-03)

**Cleanup:**
- ✅ Nhóm A: xoá 20 `chaos_drill_results_*.json`, `dist/`, `.DS_Store`, 39 `__pycache__`, `.coverage` stale + thêm `.gitignore`.
- ✅ Nhóm B: xoá `reports/rag-training-loops/`, 2 UAT report 2026-04-10, 4 `ADVISORY_MODE_*` design docs + pptx (giữ `REDTEAM_FINDINGS` làm chứng cứ).
- ✅ Nhóm C stale: gỡ `pgvector_health` khỏi `routing_policy.py` + `sop_templates.yaml`; model default `qwen3.6`→`qwen2.5-coder:7b`; gỡ chat_id khỏi CLAUDE.md; sửa nhãn `architecture.md`.

**Split-role consolidation (theo yêu cầu user):**
- ✅ Xoá 5 deployment (`omni-analyst/prober/executor/core/worker.yaml`) + 5 RBAC file split-role.
- ✅ Nhúng 6 Role/ClusterRole định nghĩa vào `omni-fullstack-rbac.yaml` (self-contained).
- ✅ Rewire Makefile: `deploy-worker`→`deploy-fullstack`; xoá `legacy-deploy-worker`, `scale-down-split`, `rollback-fullstack`; `rollback` + `deploy-prober-rbac` repoint fullstack.
- ✅ `omni-analyst-service` selector repoint `app: omni-fullstack` (giữ tên DNS).

**Fix HIGH:**
- ✅ S1: gỡ verb create/patch/update trên `clusterrolebindings` khỏi lab executor RBAC (chống self-escalation).
- ✅ T2: 5 inline `llm_num_ctx=4096`→`build_llm_options`; default code-aligned 8192 (`settings.py` + helper).
- ✅ T3: fire-and-forget task `remote_agent_pipeline.py` giữ strong ref + done-callback log exception.
- ✅ Q1: fix 5 test fixture thiếu `siem_chain_consumer_enabled`; fix 6 test RBAC trỏ file mới.

**Verify:** `pytest tests/` → **5085 passed, 0 failed** (trước: 5080/5 fail). YAML parse OK. Không còn dangling ref file đã xoá (trừ comment cố ý).

**Còn lại (chưa làm, đề xuất sprint riêng):** S3 (auth `/metrics`+webhook), S4/S6 (prod guard unrestricted + rate-limit), S5 (CRAT ở remote_agent_pipeline fallback), T1 (tách god-object `evidence_consumer.py` 2810 dòng), O1 (Redis/Kafka HA cho prod).

---
*Audit bởi 3 sub-agent + chạy suite thực tế. Pha 1 read-only; Pha 2 cleanup+fix HIGH+consolidation đã thực thi & verify.*
