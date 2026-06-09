# MASTER PLAN — Graduated Autonomy (Tiers) + Risk-Class + Telegram HITL + Admin Control UI

> Status: **step 0–8 DONE — TOÀN BỘ §6.7 hoàn tất + redeploy** (2026-06-05).
> Owner: hiendang. Tests: 5193 passed. Deployed lab: PG18 + gateway + worker + omni-ui (all rolled out).
> ✅ §6.7 xong: 5 endpoint BE mới (risk-class/flags/tenants/api-keys/hitl) + 4 panel FE
> (RiskClassMatrix/RuntimeFlags/Tenant/HitlQueue) + gỡ link Operator khỏi Admin sidebar +
> rebuild & rollout `omni-ui`. Worker `hitl_ui_decisions_loop` consume `omni-hitl-decisions`.
> Live E2E PASS: tier/readiness/2-step, risk-class invariant (dangerous→HIGH 400), flags,
> tenant+api-key (plaintext once), HITL decide idempotent, Transactional Outbox 10/10 SENT.
>
> **Quyết định #5 (2026-06-05):** Admin UI (`portal.ai-agent.local/admin`) là trang **QUẢN TRỊ**,
> không phải vận hành. Refactor TOÀN BỘ thành **control panel write-capable** — nơi setup, config,
> thao tác trực tiếp hệ thống, **persist xuyên suốt PostgreSQL** (KHÔNG read-only như hiện tại).
> **GỠ HẲN mọi link/embed sang Operator Console** khỏi Admin (sidebar `navPortal` external
> `/operator`, mọi nút "mở Operator"). Quản trị ≠ vận hành: tách bạch 2 realm.
> Nguyên tắc nền: mọi quyết định bám symbol/file đang tồn tại. Không nới bất biến nào
> (executor NEVER cluster-admin · CRAT fail-closed · kill-switch fail-closed).
>
> **Quyết định #4 (2026-06-05):** MỌI cấu hình/thay đổi/cập nhật trên Admin UI là
> **persistent vào PostgreSQL** (source-of-truth). Redis policy store hạ xuống thành
> **write-through cache** (đọc nhanh ở hot path gate). Postgres durable + relational +
> dễ audit/khôi phục; CLAUDE.md "Postgres removed" chỉ áp cho RAG, KHÔNG áp cho
> config-of-record. Dùng PostgreSQL chia sẻ với FinGuard (đã verified integration).

---

## 0. Mục tiêu

Biến Omni từ "one-shot advisory, view-only console" thành hệ tự chủ **có cấp độ**, do
operator điều khiển hoàn toàn qua UI:

| Pha | Thời gian | Quyền | Cơ chế |
|---|---|---|---|
| `shadow` | tháng 0–3 | read-only, chỉ điều tra + SUGGEST | = hành vi production hiện tại |
| `assist` | tháng 3–9 | tự chạy mutate `LOW`; `MEDIUM` → HITL Telegram | risk-class gate + Telegram 2 chiều |
| `auto` | tháng 9+ | tự chạy `LOW`+`MEDIUM`; `HIGH` luôn HITL | operator bật thủ công |

**Bất biến chuyển pha:** KHÔNG bao giờ tự nhảy tier. Chỉ operator bấm trên Admin UI.
Hệ chỉ *tính & hiển thị* chỉ số readiness, không tự hành động.

---

## 1. Trạng thái thực tế đã verify (anchors)

| Thành phần | File:dòng | Dùng làm gì |
|---|---|---|
| `AdvisoryModeKillSwitch.validate_execution_gate` | `src/workers/advisory_mode_kill_switch.py:22` | **điểm chèn gate DUY NHẤT** — mọi mutate đã đi qua đây |
| `dangerous_tools` set (6 tool) | `advisory_mode_kill_switch.py:58` | hạt giống risk-class `HIGH` |
| `omni_auto_execute_enabled` | `src/workers/settings.py:149` | flag cũ — tier **dẫn xuất**, không xoá |
| `omni_siem_suggest_only` | `settings.py:1345` | giữ nguyên |
| `READONLY_TOOLS` + `_meta_readonly` | `tool_registry.py:62`, `k8s_cluster_tools.py:29` | nguồn sự thật readonly-vs-mutate |
| `run_gated_allowlisted_execute` | `src/execution/promotion.py:43` | đường thực thi allowlist (pha assist/auto) |
| `wilson_lower_bound` | `src/workers/proactive_policy_gate.py:11` | tính readiness (chỉ hiển thị) |
| KPI keys per-tenant | `omni:kpi:z:{tenant}:accepted\|rejected\|false_positive` | nguồn data readiness thật |
| Telegram long-poll + callback | `ingest/telegram.py:56,88` · `omni_worker.py:82` | HITL 2 chiều **đã chạy** |
| HITL pipeline | `hitl_dispatcher.py`, topic `omni-hitl-pending` | đường duyệt đã có |
| Autonomy policy store (ghi được) | `src/gateway/routes/autonomy.py` (`POST /policy/rule`, `/policy/reset`, `GET /policy/history`) | **xương sống config-write** + audit history |
| CRAT event types | `src/services/audit_ledger/crat_event_types.py:5` | thêm 1 hằng mới |
| Admin UI (view-only, 116 dòng) | `ui/app/admin/page.tsx` | **làm lại toàn bộ** thành control panel |
| Ops UI | `ui/app/operator/page.tsx` | **KHÔNG đụng** |

---

## 2. Risk-Class (bảng TĨNH — không phân loại động)

Thêm field `metadata["risk_class"]` tại chỗ `@register_tool`, đọc qua `risk_class_of(tool_name)`
(fail-closed = `HIGH` nếu thiếu).

| Tool thực | risk_class | Lý do |
|---|---|---|
| mọi `_meta_readonly` (tail_logs, get_logs, describe, get_events, list_*, check_endpoints, verify_rollout, get_deployment_state, list_workload_pods, get_pod_secret_refs, get_secret_keys) | `READONLY` | không đổi state |
| `k8s_rollout_restart` | `LOW` | idempotent, tự phục hồi |
| `k8s_create_or_patch_configmap` | `LOW` | **cố định** (bỏ phân loại động — quyết định #3) |
| `k8s_scale_resource` / `k8s_scale_deployment` | `MEDIUM` | đổi capacity; chặn scale-to-0 |
| `k8s_patch_resource` / `k8s_patch_configmap` | `MEDIUM` | sửa config sống có thể hồi quy |
| `k8s_apply_rbac_least_privilege` | `MEDIUM` | đổi RBAC (chỉ siết quyền) |
| `k8s_delete_pod` | `HIGH` | mất pod (đã trong dangerous_tools) |
| `k8s_patch_secret` | `HIGH` | đụng secret (đã dangerous_tools) |
| `k8s_delete_deployment` / `delete_pvc` / `patch_rbac` / `mutate_taint` | `HIGH` | đã dangerous_tools |

`dangerous_tools` hiện tại = tập con `HIGH`. Risk-class chỉ mở rộng thành 4 mức, không mâu thuẫn.

**Risk-class sửa được trên Admin UI** (ghi vào policy store) — operator override mặc định nếu cần.

---

## 3. `OMNI_AUTONOMY_TIER` — 1 trục điều khiển

`settings.py` thêm:
```
OMNI_AUTONOMY_TIER: Literal["shadow","assist","auto"] = "shadow"   # default fail-closed
```

Dẫn xuất tương thích ngược (khi tier chưa set):
- `auto_execute_enabled=False` → `shadow`
- `auto_execute_enabled=True`  → `auto`

Giá trị tier **runtime**: đọc Redis cache trước (hot path), Redis được nạp write-through
từ **PostgreSQL** (source-of-truth) — env chỉ là default khi DB rỗng lần đầu.
→ Đổi tier trên UI: ghi Postgres → đẩy Redis → có hiệu lực ngay, không redeploy.
Pod restart đọc lại từ Postgres, không mất cấu hình (Redis có thể bay).

### Logic gate (mở rộng `validate_execution_gate`, giữ chữ ký cũ)
```
risk = risk_class_of(tool_name)            # fail-closed HIGH
if risk == READONLY:        ALLOW
if tier == shadow:          BLOCK  -> SUGGEST            (= hành vi hiện tại)
if risk == HIGH:            BLOCK  -> HITL Telegram      (mọi tier)
if tier == assist:
    LOW    -> ALLOW
    MEDIUM -> BLOCK -> HITL Telegram
if tier == auto:
    LOW|MEDIUM -> ALLOW
```
Mọi BLOCK→HITL/SUGGEST không drop thầm; HIGH luôn cần người.

---

## 4. Telegram HITL 2 chiều (1 kênh của operator)

**Quyết định:** chỉ chạy 1 kênh Telegram cá nhân. FinGuard customer HITL = phát triển sau,
KHÔNG làm trong phase này.

Luồng:
```
mutate cần duyệt (MEDIUM@assist, hoặc HIGH mọi tier)
  → emit omni-hitl-pending
  → hitl_dispatcher gửi Telegram card + inline_keyboard [✅ Approve][❌ Reject]
        callback_data = "hitl:{decision}:{pending_id}"
  → operator bấm nút
  → _handle_telegram_fallback_callback (omni_worker.py:82) bắt callback_query
        APPROVED → omni-actions      (executor chạy)
        REJECTED → omni-action-feedback (analyst học)
  → answer_callback_query xác nhận
  → CRAT HITL_DECISION ghi TRƯỚC khi dispatch (fail-closed)
  → timeout OMNI_HITL_ESCALATION_TIMEOUT_SEC (900s) → auto-reject, không treo
```
Hạ tầng tái dùng 100%. Việc mới: render inline_keyboard cho HITL card + map prefix
`callback_data="hitl:*"` trong handler đã có.

---

## 5. Cổng Readiness (CHỈ hiển thị — không tự hành động)

`tier_readiness()` tính từ data thật, trả về cho UI:
```
shadow→assist ready khi:
  elapsed_days >= OMNI_TIER_MIN_DAYS_SHADOW (default 90)
  total_advisories >= N_min
  wilson_lower_bound(accepted, total) >= 0.80          # KPI keys thật
assist→auto ready khi:
  elapsed_days >= 270 (kể từ shadow)
  wilson_lower_bound(LOW_mutate_success, total) >= 0.85 # omni-action-feedback
  false_positive_rate < ngưỡng
```
Output: metric `omni_tier_promotion_ready{from,to}` (0/1) + số liệu chi tiết lên UI.
**Không** có code path nào tự set tier từ kết quả này.

---

## 6. Admin UI — REFACTOR TOÀN BỘ thành Control Panel (write-capable, PostgreSQL)

> **Bản chất:** Admin `/admin` = trang **QUẢN TRỊ** hệ thống — nơi setup, config, thao tác
> trực tiếp, **ghi persistent vào PostgreSQL** (source-of-truth). KHÔNG còn read-only.
> Operator `/operator` = trang **VẬN HÀNH** (xem incident, advisory, HITL realtime) — realm riêng.
> **Tách bạch tuyệt đối:** Admin KHÔNG được link/embed/redirect sang Operator. Gỡ:
> - `ui/components/sidebar.tsx` `navPortal[]` mục `{ external: //${OMNI_HOST}/operator, "Operator Console" }` (dòng 57) + section "Console" (dòng 56).
> - Mọi nút/anchor "mở Operator" trong các panel admin (nếu có).
> Style: dark-luxury, amber accent, monospace. Mọi ghi: `X-API-Key` master + CRAT audit +
> xác nhận 2 bước cho hành động tăng quyền (nâng tier, hạ risk dangerous).

### 6.1. Phân loại lại panel: VIEW-ONLY hiện tại → WRITE-CAPABLE

Admin hiện có 8 panel **đa số read-only** (`useAdminData`): Workers, KPI, CRAT, LlmRag,
Autonomy(history view), RemoteAgents, Deploy, TierControl(panel duy nhất write). Refactor:

| Panel | Hiện tại | Sau refactor (write-capable, PostgreSQL) | Endpoint backend |
|---|---|---|---|
| **Tier Control** | ✅ write | giữ; readiness gauge; 2-step nâng tier; hạ tức thì | `GET/POST /autonomy/tier` ✅ |
| **Risk-Class Matrix** | ❌ chưa có | bảng tool × risk_class, ô bấm override (RO/LOW/MED/HIGH), khoá HIGH cho dangerous, 2-step khi hạ | `GET/POST /autonomy/risk-class` (mới) |
| **Runtime Flags** | ❌ chưa có | form typed: auto_execute, siem_suggest_only, HITL timeout, num_ctx, model... bool/int/str; show nguồn DB vs env | `GET/POST /autonomy/flags` (mới) |
| **Tenant / API Keys** | ❌ chưa có | CRUD tenant + rotate key (hash, chỉ hiện prefix + created_at) | `GET/POST/DELETE /autonomy/tenants` + `/api-keys` (mới) |
| **HITL Queue** | ⚠️ proxy Redis | duyệt approve/reject trên UI song song Telegram (CRAT trước dispatch) | `GET /autonomy/hitl/pending` + `POST /autonomy/hitl/{id}/decide` (mới) |
| **RAG / SOP Control** | ⚠️ view HLEN | trigger re-ingest, promote/demote SOP | proxy `ui/app/api/playbooks` (có sẵn) |
| **Deploy** | ⚠️ view | giữ (đã có action) | proxy có sẵn |
| **Observability** (Workers/KPI/CRAT/LlmRag/RemoteAgents/Pipeline) | read | giữ read-only, gom nhóm **OBSERVABILITY** tách khỏi **CONFIG** | đọc, không đổi |

> Mọi write CONFIG đi qua gateway → `AdminConfigRepo` → PostgreSQL (`config_change_log` cùng TX)
> → write-through Redis cache → CRAT outbox. KHÔNG ghi thẳng Redis (xem §6.5).

### 6.6. Trạng thái thực tế (2026-06-05) — GAP so với "LÀM LẠI TOÀN BỘ"

> Lần code đầu **CHƯA** rebuild toàn bộ; chỉ bolt-on 1 panel `TierControlPanel` vào dashboard
> view-only cũ, và **chưa rebuild/redeploy image `omni-ui`** nên portal vẫn hiển thị UI cũ.
> Đây là nợ kỹ thuật phải trả: §6 yêu cầu **control panel hoàn chỉnh**, không phải 1 panel.

| Panel | BE (PostgreSQL) | BE endpoint | FE panel | Deployed |
|---|---|---|---|---|
| Tier Control | ✅ `autonomy_tier_state` | ✅ `GET/POST /autonomy/tier` + `/readiness` | ✅ `TierControlPanel.tsx` | ❌ chưa build omni-ui |
| Risk-Class Matrix | ✅ repo `set_risk_class_override` | ❌ **thiếu** `GET/POST /autonomy/risk-class` | ❌ chưa có panel ghi | ❌ |
| Runtime Flags | ✅ repo `set_runtime_flag` | ❌ **thiếu** `GET/POST /autonomy/flags` | ❌ | ❌ |
| HITL Live Queue | ⚠️ `hitl_decision` ledger | ⚠️ proxy Redis cũ, chưa PostgreSQL | ❌ panel ghi UI | ❌ |
| Tenant / API Keys | ✅ `tenant`/`tenant_api_key` | ❌ **thiếu** `/autonomy/tenants` (PostgreSQL) | ❌ | ❌ |
| RAG / SOP Control | n/a (Redis) | ⚠️ proxy cũ | ❌ panel ghi | ❌ |
| Live Observability | — | — | ✅ giữ panel cũ (gom nhóm OBSERVABILITY) | ✅ |
| **Gỡ link Operator** | — | — | ❌ `sidebar.tsx` `navPortal` vẫn external `/operator` | ❌ |

### 6.7. Việc còn lại để hoàn tất §6 (full rebuild)

**Backend (gateway, PostgreSQL-backed, KHÔNG import workers):**
1. `GET/POST /autonomy/risk-class` — list bảng tĩnh §2 + override; ghi qua `repo.set_risk_class_override` (đã chặn hạ dangerous < HIGH).
2. `GET/POST /autonomy/flags` — runtime flags qua `repo.set_runtime_flag` (+ Redis invalidate).
3. `GET/POST/DELETE /autonomy/tenants` + `/api-keys` — tenant/key qua repo (hash key, không plaintext).
4. `GET /autonomy/hitl/pending` + `POST /autonomy/hitl/{id}/decide` — duyệt trên UI song song Telegram (CRAT trước dispatch, tái dùng `hitl_telegram.handle_*` logic).
5. Proxy Next.js tương ứng dưới `ui/app/api/autonomy/{risk-class,flags,tenants,hitl}/route.ts`.

**Frontend (rebuild `ui/app/admin/` thành control panel thực thụ):**
6. Layout control-first: nhóm **CONFIG** (Tier · Risk Matrix · Flags · Tenant · HITL) lên đầu, tách rõ khỏi **OBSERVABILITY** (Workers · KPI · CRAT · LlmRag · RemoteAgents · Pipeline) ở dưới.
7. `RiskClassMatrixPanel` — bảng tool × risk_class, ô bấm để override (READONLY/LOW/MEDIUM/HIGH), khoá HIGH cho dangerous_tools, 2-step khi hạ rủi ro.
8. `RuntimeFlagsPanel` — form key/value typed (bool toggle, int/str input), hiển thị nguồn (DB vs env default).
9. `TenantPanel` — CRUD tenant + rotate API key (chỉ hiện prefix + thời điểm tạo).
10. `HitlQueuePanel` — danh sách pending + nút Approve/Reject + countdown timeout.
11. Style nhất quán dark-luxury/amber/mono; mọi ghi hiện toast trạng thái + lỗi rõ ràng.
12. **Gỡ liên kết Operator khỏi Admin realm:** xoá mục external `/operator` + section "Console" trong `sidebar.tsx` `navPortal[]`; rà các panel admin không còn anchor sang `/operator`. Admin = quản trị thuần.

**Deploy (BẮT BUỘC — lần trước bỏ sót):**
13. `docker build -t omni-ui:latest -f ui/Dockerfile ui/` → `kubectl rollout restart deployment/omni-ui` → verify trên `portal.ai-agent.local` (UI mới hiện, không còn link Operator).

---

## 6.5. PostgreSQL Schema — Admin Config Store (source-of-truth)

> Schema `omni_admin`. Mọi bảng config có `updated_by`, `updated_at`, `version` (optimistic
> lock). Ghi theo pattern **write-through**: TX Postgres commit → set Redis cache → CRAT block.
> Nếu Postgres ghi fail → abort, KHÔNG đụng Redis (fail-closed, nhất quán với kill-switch/CRAT).
> Mọi write đi qua `config_change_log` trong CÙNG transaction (atomic audit).

```sql
CREATE SCHEMA IF NOT EXISTS omni_admin;

-- 1. Tier hiện tại (1 hàng / tenant). Source-of-truth cho OMNI_AUTONOMY_TIER.
CREATE TABLE omni_admin.autonomy_tier_state (
    tenant_id     TEXT PRIMARY KEY,
    tier          TEXT NOT NULL CHECK (tier IN ('shadow','assist','auto')),
    updated_by    TEXT NOT NULL,                  -- actor (master API key id / operator)
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    version       BIGINT NOT NULL DEFAULT 1,
    crat_ref      TEXT                            -- hash block AUTONOMY_TIER_CHANGED
);

-- 2. Lịch sử đổi tier (append-only, đối chiếu CRAT chain).
CREATE TABLE omni_admin.autonomy_tier_history (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    from_tier     TEXT,
    to_tier       TEXT NOT NULL,
    actor         TEXT NOT NULL,
    wilson_lb     DOUBLE PRECISION,               -- readiness lúc đổi (snapshot)
    accepted      INTEGER,
    total         INTEGER,
    elapsed_days  INTEGER,
    forced        BOOLEAN NOT NULL DEFAULT false,  -- nâng tier khi readiness chưa đạt
    crat_ref      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_tier_hist_tenant ON omni_admin.autonomy_tier_history(tenant_id, created_at DESC);

-- 3. Override risk-class theo tool (mặc định = bảng tĩnh §2; bảng này chỉ chứa override).
CREATE TABLE omni_admin.risk_class_override (
    tenant_id     TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    risk_class    TEXT NOT NULL CHECK (risk_class IN ('READONLY','LOW','MEDIUM','HIGH')),
    reason        TEXT,
    updated_by    TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    version       BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, tool_name)
);
-- BẤT BIẾN: không cho hạ dangerous_tools xuống dưới HIGH (enforce ở app layer + trigger).

-- 4. Runtime flags (auto_execute, siem_suggest_only, HITL timeout, num_ctx, model...).
CREATE TABLE omni_admin.runtime_flag (
    tenant_id     TEXT NOT NULL,
    flag_key      TEXT NOT NULL,                  -- vd 'omni_hitl_escalation_timeout_sec'
    flag_value    JSONB NOT NULL,                 -- giữ kiểu (int/bool/str/obj)
    value_type    TEXT NOT NULL,                  -- 'int'|'bool'|'str'|'float'|'json'
    updated_by    TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    version       BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, flag_key)
);

-- 5. Tenant + API keys (thay/bổ sung cho OMNI_TENANT_APIKEYS env tĩnh).
CREATE TABLE omni_admin.tenant (
    tenant_id     TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE omni_admin.tenant_api_key (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL REFERENCES omni_admin.tenant(tenant_id),
    key_hash      TEXT NOT NULL,                  -- CHỈ lưu hash (argon2/sha256), KHÔNG plaintext
    key_prefix    TEXT NOT NULL,                  -- 8 ký tự đầu để hiển thị/nhận diện
    label         TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
    created_by    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX ux_api_key_hash ON omni_admin.tenant_api_key(key_hash);
CREATE INDEX ix_api_key_tenant ON omni_admin.tenant_api_key(tenant_id) WHERE status='active';

-- 6. HITL decision ledger (bổ sung CRAT, để query/UI nhanh — CRAT vẫn là chain bất biến).
CREATE TABLE omni_admin.hitl_decision (
    pending_id    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    risk_class    TEXT NOT NULL,
    tier_at_time  TEXT NOT NULL,
    decision      TEXT NOT NULL CHECK (decision IN ('PENDING','APPROVED','REJECTED','TIMEOUT')),
    channel       TEXT NOT NULL DEFAULT 'telegram' CHECK (channel IN ('telegram','ui')),
    actor         TEXT,
    crat_ref      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at    TIMESTAMPTZ
);
CREATE INDEX ix_hitl_tenant_state ON omni_admin.hitl_decision(tenant_id, decision, created_at DESC);

-- 7. Audit phổ quát: MỌI write config UI ghi 1 dòng (cùng TX với bảng đích).
CREATE TABLE omni_admin.config_change_log (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    entity        TEXT NOT NULL,                  -- 'tier'|'risk_class'|'runtime_flag'|'tenant'|'api_key'
    entity_key    TEXT,                           -- vd tool_name / flag_key
    action        TEXT NOT NULL,                  -- 'create'|'update'|'delete'
    old_value     JSONB,
    new_value     JSONB,
    actor         TEXT NOT NULL,
    crat_ref      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_cfg_log_tenant ON omni_admin.config_change_log(tenant_id, created_at DESC);

-- 8. CRAT outbox (Transactional Outbox Pattern — atomic tuyệt đối config↔CRAT).
--    Payload CRAT được ghi CÙNG TX với bảng đích → không bao giờ có config thiếu
--    CRAT event đã enqueue. Background drainer đọc PENDING → write_audit_block →
--    cập nhật crat_ref + status=SENT. Retry an toàn (at-least-once, CRAT idempotent
--    theo dedup_key). Fix dual-write gap: COMMIT-rồi-mới-gọi-CRAT.
CREATE TABLE omni_admin.crat_outbox (
    id            BIGSERIAL PRIMARY KEY,
    dedup_key     TEXT NOT NULL UNIQUE,           -- {entity}:{entity_key}:{version} — idempotent
    event_type    TEXT NOT NULL,                  -- vd AUTONOMY_TIER_CHANGED
    payload       JSONB NOT NULL,                 -- snapshot bất biến để CRAT hash
    status        TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENT','FAILED')),
    attempts      INT  NOT NULL DEFAULT 0,
    last_error    TEXT,
    crat_ref      TEXT,                            -- block hash sau khi SENT
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at       TIMESTAMPTZ
);
CREATE INDEX ix_crat_outbox_pending ON omni_admin.crat_outbox(status, created_at) WHERE status <> 'SENT';
```

**Quan hệ Postgres ↔ Redis ↔ CRAT (write path — Transactional Outbox):**
```
UI POST → validate (X-API-Key master) →
  BEGIN TX:
    UPSERT bảng đích (version+1, updated_by)
    INSERT config_change_log (old/new, actor)
    INSERT crat_outbox (dedup_key, event_type, payload, status='PENDING')   ← atomic CÙNG TX
  COMMIT                                          ← cấu hình + audit + CRAT-intent cùng durable
  → Redis SET cache key (write-through; Redis fail chỉ log, Postgres vẫn đúng)

[CRAT drainer — background loop, role=analyst/full]:
  SELECT ... WHERE status='PENDING' FOR UPDATE SKIP LOCKED
  → write_audit_block(payload)  (fail-closed; lỗi → attempts++, last_error, retry)
  → UPDATE status='SENT', crat_ref, sent_at
```
> **Vì sao đúng:** CRAT-intent nằm trong CÙNG TX với config → không tồn tại trạng thái
> "config đã lưu nhưng chưa enqueue CRAT". Drainer at-least-once + `dedup_key` UNIQUE
> đảm bảo CRAT chain không nhân đôi. Khác hot-path advisory dispatch (CRAT vẫn ghi
> đồng bộ fail-closed TRƯỚC khi dispatch action) — config path không chặn latency nên
> dùng outbox để đạt atomic tuyệt đối thay vì dual-write.

**Đọc ở hot path gate (`validate_execution_gate`):** Redis cache → miss thì fallback Postgres
→ miss thì env default. KHÔNG query Postgres mỗi mutate (cache TTL + invalidations khi ghi).

**Migration:** `migrations/omni_admin/0001_init.sql` (raw SQL, async `asyncpg`/`psycopg`).
Idempotent `CREATE ... IF NOT EXISTS`. Seed tier='shadow' cho tenant 'default'.

---

## 7. Audit & Observability

- CRAT thêm `CRAT_EVENT_AUTONOMY_TIER_CHANGED` — payload {from, to, actor, wilson_lb, accepted, total, elapsed_days}. Fail-closed.
- Mọi ghi config UI → atomic 1 TX: `config_change_log` + `crat_outbox` (Transactional
  Outbox) cùng bảng đích; sau COMMIT chỉ còn write-through Redis cache. CRAT block do
  drainer ghi từ outbox (at-least-once, `dedup_key` UNIQUE). KHÔNG dual-write: không tồn
  tại trạng thái "config đã lưu nhưng CRAT chưa enqueue".
- CRAT drainer chạy ở role `analyst`/`full` (loop riêng); metric `omni_crat_outbox_pending`
  + alert khi PENDING tồn > N phút (CRAT chain tụt hậu so với config).
- Metrics mới (style `metrics_exporter.py`):
  `omni_autonomy_tier{tier}` · `omni_tier_gate_blocked_total{tier,risk_class,reason}` · `omni_tier_promotion_ready{from,to}`.
- Alert (mẫu `prometheus-rules-omni-health.yaml`): cảnh báo khi `tier_promotion_ready=1`.

---

## 8. Thứ tự thực thi (khi bắt đầu code)

| # | Phạm vi | Test/Bất biến phải giữ |
|---|---|---|
| 0 | **PostgreSQL schema** `omni_admin` (§6.5, gồm `crat_outbox`) + migration 0001 + async repo layer (asyncpg) + write-through cache helper + **CRAT outbox drainer** (FOR UPDATE SKIP LOCKED) | migration idempotent; seed tier=shadow; repo test (Postgres fail → abort, Redis không đụng); drainer test (enqueue→SENT, retry idempotent qua `dedup_key`) |
| 1 | `risk_class` tĩnh + `risk_class_of()` fail-closed HIGH (đọc override từ DB→cache) | mọi tool có class; thiếu→HIGH |
| 2 | `OMNI_AUTONOMY_TIER` + dẫn xuất từ flag cũ + đọc Postgres(cache) runtime | default shadow ≡ auto_execute=False; test cũ pass |
| 3 | Mở rộng `validate_execution_gate` (ma trận tier×risk) | dangerous_tools chặn ở MỌI tier; ma trận 3×4 test |
| 4 | Telegram HITL inline (card + callback `hitl:*`) | CRAT HITL_DECISION trước dispatch; timeout auto-reject |
| 5 | `tier_readiness()` + metrics (chỉ hiển thị) | không có path tự set tier |
| 6 | `POST /autonomy/tier` → 1 TX (UPSERT tier + config_change_log + crat_outbox enqueue) + Redis invalidate; drainer ghi CRAT TIER_CHANGED | X-API-Key bắt buộc; atomic config↔CRAT-intent; không dual-write |
| 7 | **Refactor TOÀN BỘ Admin UI** thành control panel write-capable, PostgreSQL-persisted — ⚠️ CHƯA XONG (xem §6.1/§6.6/§6.7): mới Tier Control; thiếu Risk/Flags/Tenant/HITL endpoint+panel; chuyển panel view-only→write; **gỡ link Operator khỏi Admin**; **chưa redeploy omni-ui** | Ops UI không đổi; Admin không còn link Operator; 2-step confirm nâng tier/hạ risk |
| 8 | Bỏ test phân loại động configmap (sai thực tế) | grep & xoá case liên quan |

---

## 9. Done-criteria

- [ ] Default `shadow` = production hiện tại, toàn bộ test cũ pass (5085+).
- [ ] Không tool nào thiếu risk_class mà lọt (fail-closed HIGH).
- [ ] `HIGH`/dangerous_tools bị chặn ở mọi tier → HITL.
- [ ] `assist`: LOW tự chạy, MEDIUM → Telegram approve/reject thật, có CRAT.
- [ ] Đổi tier CHỈ qua Admin UI; mỗi lần có CRAT `AUTONOMY_TIER_CHANGED`.
- [ ] Admin UI cấu hình được: tier, risk-class, runtime flags, HITL, tenant — không còn view-only.
- [ ] MỌI write config UI persistent vào PostgreSQL (`omni_admin`); restart pod/xoá Redis → cấu hình KHÔNG mất (đọc lại từ Postgres).
- [ ] Mỗi write có 1 dòng `config_change_log` + 1 `crat_outbox` (atomic CÙNG TX); drainer ghi CRAT block, không dual-write.
- [ ] Postgres ghi fail → abort, Redis/state không lệch (fail-closed).
- [ ] Kill CRAT drainer giữa chừng → restart vẫn ghi đủ block (outbox PENDING không mất, `dedup_key` chống nhân đôi).
- [ ] Ops UI nguyên vẹn.
- [ ] Không nới bất biến: executor non-cluster-admin, CRAT fail-closed, kill-switch fail-closed.

---

## 10. Ngoài phạm vi (làm sau)

- FinGuard customer HITL multi-approver (giữ `hitl_dispatcher` FinGuard API path, chưa kích hoạt).
- Active OS/service probing (disk/mysql) thành tool động trong TOOL_REGISTRY — chuỗi điều tra
  xuyên tầng dưới K8s (đã phân tích: hiện chỉ validate thụ động qua `os_state_validator.py`).
  Đây là epic riêng, sẽ mở khi tier-system ổn.
