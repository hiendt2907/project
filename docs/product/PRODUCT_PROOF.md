# Product Proof

Tài liệu này KHÔNG phải bằng chứng tự thân — mỗi dòng phải trỏ tới lệnh/API/log/datastore query
thật đã chạy. Cập nhật sau mỗi iteration của Continuous Productization Loop.

## Environment

- Commit: `67423b9` (tại thời điểm build) → image rebuild sau đó không đổi source, chỉ đổi digest
- Build: `multi-agent-system:latest`, digest `sha256:c2d433daac77e0ec4c4c474bc011b2000bd22fbf962117a18610126fbb44e9f6`
- Namespace: `multi-agent` (OrbStack k8s, single node `orbstack`)
- Tenant: `staging-sim` (provisioned qua `AdminConfigRepo.create_tenant()`, xem drift-correction post-mortem)
- VMs: `cust-edge` (192.168.139.87), `cust-app` (192.168.139.237), `cust-db` (192.168.139.225) — OrbStack, Ubuntu 24.04.4 arm64
- Agents: `staging-sim_cust-edge`, `staging-sim_cust-app`, `staging-sim_cust-db` — systemd unit `omni-remote-agent.service` trên cả 3 VM
- Last verified: 2026-07-02, iteration 4 của Continuous Productization Loop

## Capability Matrix

| Capability | Code | Deployed | Runtime verified | Operator-visible | Evidence |
|---|---:|---:|---:|---:|---|
| Tenant creation | ✅ | ✅ | ✅ | ⚠️ (API only, không UI) | `omni_admin.tenant` row `staging-sim` — `psql -c "SELECT * FROM omni_admin.tenant"` |
| Agent enrollment | ✅ | ✅ | ✅ | ❌ | `/var/log/omni-agent.log` trên cả 3 VM: `POST .../webhook/agent/register "HTTP/1.1 200 OK"` |
| Agent heartbeat | ✅ | ✅ | ✅ | ❌ | Log trên `cust-app`: `register` lặp lại mỗi ~2 phút (ttl=120), `GET .../commands/...` mỗi ~5s |
| Continuous discovery (cust-edge, cust-db, cust-app) | ✅ | ✅ | ✅ **3/3 host** | ❌ | `omni:evrl:p:staging-sim_{cust-edge,cust-db,cust-app}:{process_list,port_scan,service_topology}:PASSED` tồn tại cho cả 3 host trong Redis |
| Discovery evidence transport (Kafka) | ✅ | ✅ | ✅ | ❌ | `kafka-consumer-groups.sh --describe --group omni-onboarding-discovery` → lag=0, offset tăng liên tục |
| Observation → Fact projection | ✅ (code từ `1bc6292`) | ✅ (sau redeploy iteration 1) | ✅ | ❌ | log `onboarding_pipeline: system_model contradiction tenant=staging-sim` + Redis `omni:aoip:system_model:staging-sim` |
| System Twin persisted | ✅ | ✅ | ✅ **3/3 host** | ❌ (chỉ đọc được qua `redis-cli`) | `HGETALL omni:aoip:system_model:staging-sim` → revision=54, 76 facts (cust-edge 38, cust-db 19, cust-app 19); `host:cust-app` → `exposes_port 8080` khớp `ss -lntp` trên VM |
| Competency Matrix | ✅ | ✅ | ⚠️ chưa test riêng trong iteration này | ❌ | chưa kiểm trong iteration 1 |
| Unknown/Question lifecycle (O2B) — Human Claim | ✅ | ✅ | ✅ **VERIFIED_RUNTIME iter 15** | ✅ (qua API) | `POST /onboarding/questions/{id}/answer` trên câu hỏi PENDING thật của `staging-sim` (`bdb9bb5e66be555d1fd3dd80`, facet `business_capability` của `svc:nginx`) → `status=ANSWERED`, `answer_id=a8ddaa6bd49e2f83b9cb`; `GET /onboarding/competency?...&entity_id=svc:nginx` sau đó trả facet `business_capability: state=CLAIMED, evidence_refs=["human:iter15-productizer","question:bdb9bb5e66be555d1fd3dd80"]` — đúng thiết kế (Claim KHÔNG tự thành VERIFIED, cần Fact máy khớp mới promote). Test: `tests/test_aoip_question_lifecycle.py` + `tests/test_gateway_onboarding_competency_routes.py` (19 passed) |
| Onboarding readiness | ✅ | ✅ | ✅ | ⚠️ (đọc DB trực tiếp) | `omni_admin.tenant_readiness_state` có row `staging-sim`, `readiness_flag=false` |
| Competency Matrix API (`GET /onboarding/competency`) | ✅ | ✅ (sau fix iteration 3) | ✅ | ✅ | `curl -H "Authorization: Bearer $KEY" ".../onboarding/competency?tenant_id=staging-sim&entity_type=host&entity_id=host:cust-app"` → `identity: VERIFIED`, evidence_refs trỏ `discovery:port_scan/process_list` thật |
| Unknowns API (`GET /onboarding/unknowns`) | ✅ | ✅ (sau fix iteration 3) | ✅ | ✅ | `curl .../onboarding/unknowns?tenant_id=staging-sim` → trả Unknown thật (vd `svc:fsidd` facet `business_capability`) |
| Mission/Command/Execution (closed-loop mutation) | ✅ code tồn tại | ✅ | ❌ chưa test | ❌ | Ngoài phạm vi golden journey hiện tại — `OMNI_AUTO_EXECUTE_ENABLED=false` cố ý |
| Fact provenance có agent_id thật | ✅ (fix iteration 4) | ✅ | ✅ | ✅ (qua `/onboarding/competency`) | Root cause: `_project_into_system_model` đọc `ev_doc.get("agent_id")` sai vị trí (thật ra nằm trong `extracted_fact`). Fix 2 lớp: `onboarding_pipeline.py` đọc đúng vị trí + `schema.py` promote `agent_id`/`hostname` lên top-level trước khi truncate. Verify: `redis-cli HGET omni:aoip:system_model:staging-sim facts` → 0/76 fact còn `agent:unknown` |
| Handover-doc upload (A8, `POST /onboarding/handover-doc`) | ✅ | ✅ | ✅ **VERIFIED_RUNTIME iter 16** | ✅ (qua API) | `POST /onboarding/handover-doc` trên `staging-sim` thật → diagram version 6747→6752; `GET /onboarding/doc` sau đó chỉ chứa `content_hash`/`content_length`, KHÔNG có raw content (xác nhận `INV_DATA_RESIDENCY`). Test: `tests/test_onboarding_pipeline.py -k handover` (3 passed) |

## Golden Journey

### Tenant onboarding (staging-sim, 3 VM lab)

**Status: PARTIAL** (nâng từ 2/3 → 3/3 host sau iteration 2; operator visibility vẫn là gap chính còn lại)

1. ✅ Tenant `staging-sim` tồn tại trong `omni_admin.tenant` (tạo qua `AdminConfigRepo.create_tenant()`).
2. ✅ 3 Agent registered — log `agent/register 200 OK` trên cả 3 VM, `omni:remote_agent:registry:staging-sim_{cust-edge,cust-app,cust-db}` tồn tại trong Redis.
3. ⚠️ Agent online: chưa kiểm tra "stale/offline" threshold trong iteration này (out of scope).
4. ✅ Inventory hiển thị ĐỦ 3/3 host (`cust-edge`, `cust-db`, `cust-app`) qua Twin.
5. ✅ Services/ports phát hiện đúng cho cả 3 host: `host:cust-db` → `exposes_port 3306` (mariadbd), `exposes_port 6379` (redis-server); `host:cust-edge` → `runs_process nginx` (port 80); `host:cust-app` → `exposes_port 8080` — cả 3 khớp `ss -lntp` chạy trực tiếp trên VM qua `orb -m`.
6. ✅ Twin revision hiển thị và tăng theo thời gian thực (6 → 18 → 54 qua 2 iteration), provenance có `discovery:{probe}:{trace_id}` + `agent:unknown` (⚠️ gap nhỏ — `to_observation()` không điền đúng `agent_id`, chỉ ghi placeholder).
7. ❌ Unknowns/contradictions: có contradiction thật (`runs_service` bị ghi đè giữa các probe khác nhau trên `cust-edge`) nhưng CHƯA verify hiển thị qua API/operator surface nào.
8. ✅ Operator nay CÓ cách xem — `GET /onboarding/competency?tenant_id=...&entity_type=host&entity_id=host:cust-app` (cần `entity_id` đúng format `{type}:{id}` khớp subject trong Twin, ví dụ nhỏ nhưng dễ nhầm) trả về facet/state/evidence/confidence thật; `GET /onboarding/unknowns` trả Unknown thật. **API tồn tại và hoạt động — CHƯA có UI**, ghi rõ PARTIAL cho phần UI.

## Known Broken Links

1. ~~Chưa có UI đọc Twin/Competency~~ — **FIXED iteration 19**: trang `/understanding` trên omni-ui (entities/competency/unknowns/questions/readiness), xem bên dưới.
2. Kafka `PartitionCount=1` toàn hệ thống — chưa sửa (P1 riêng, xem drift-correction post-mortem).
3. **Chỉ `cust-app` bị thiếu discovery flag lúc provision** (`OMNI_REMOTE_DISCOVERY_ENABLED` không có trong `run.env`, trong khi cust-edge/cust-db có) — đã fix trực tiếp trên VM (`echo >> run.env` + `systemctl restart`), nhưng đây là fix runtime, CHƯA có cơ chế provisioning tự động đảm bảo VM mới không rơi vào tình trạng tương tự (gap ở `scripts/e2e_orbstack_fleet.py`/agent bundle provisioning).
4. ~~`entity_id` format nội bộ khó đoán~~ — **MITIGATED iteration 19**: `GET /onboarding/entities` + UI entity list cung cấp sẵn đúng `entity_id`, operator không còn phải tự gõ (API contract giữ nguyên).
5. ~~`coerce_evidence_dict()` cắt cứng `extracted_fact` ở 2000 ký tự~~ — **FIXED iteration 5**, xem bên dưới.
6. ~~Tenant provisioning KHÔNG idempotent~~ — **FIXED iteration 6**: `create_tenant(..., idempotent=True)` opt-in param, xem bên dưới. API HTTP `POST /autonomy/tenants` (gateway) vẫn giữ nguyên semantics 409 cũ (không set `idempotent=True`) — không phá contract hiện có, chỉ mở đường cho caller nội bộ (provisioning tooling) dùng repeat-safe path.
7. ~~Chưa có fresh-tenant runtime proof~~ — **PARTIAL iteration 7**: Phase 4 (repeat-provisioning
   proof trên Postgres thật) DONE, xem bên dưới. Phase 6-7 (golden journey Tenant→Twin→Competency
   không sửa tay cho tenant mới, cross-tenant isolation proof, operator read-only flow) CHƯA chạy —
   `tenant-replay-01` mới có row `omni_admin.tenant`, CHƯA có Agent/VM thật gắn vào tenant này.

## Iteration 9 — Phase 6 slice: real Agent+VM for tenant-replay-01, cross-tenant isolation proof (2026-07-02)

**Bottleneck đã fix (Phase 6 của slice "Repeatable Tenant Onboarding Baseline"):**

Trước iteration này `tenant-replay-01` chỉ có 1 row `omni_admin.tenant` (iteration 7) — chưa có
Agent/VM thật nào gắn vào, nên golden journey Agent→Discovery→Fact→Twin→Competency chưa từng chạy
cho tenant này và chưa có bằng chứng cách ly (isolation) giữa 2 tenant trong cùng cluster.

Không tạo VM mới (chỉ có 3 VM lab: cust-edge/cust-app/cust-db, đều đã gắn `staging-sim`) — dùng lại
`cust-edge`, cài **agent thứ hai song song** (identity riêng, install dir riêng, systemd unit
riêng) bind vào `tenant-replay-01`:

1. Thêm API key `tenant-replay-01` vào `omni-gateway-secret` (`OMNI_TENANT_APIKEYS`), rolling
   restart `omni-gateway` — verify `kubectl rollout status` xanh.
2. Cài `/opt/omni-remote-agent-replay01` trên `cust-edge` (copy venv từ agent staging-sim có sẵn),
   `run.env` mới (`OMNI_AGENT_ID=tenant-replay-01_cust-edge`, `OMNI_AGENT_TENANT_ID=tenant-replay-01`),
   systemd unit `omni-remote-agent-replay01.service` riêng — `systemctl enable --now`.
3. Runtime proof (log agent, không chỉ code/deploy):
   `/var/log/omni-agent-replay01.log` → `POST .../webhook/agent/register "HTTP/1.1 200 OK"`,
   `POST .../webhook/agent/profile "HTTP/1.1 200 OK"`, `POST .../webhook/agent/evidence` →
   `emitted evidence enqueued=6`.
4. Twin persisted thật: `redis-cli HGETALL omni:aoip:system_model:tenant-replay-01` → 41 facts,
   `revision=6`, subject duy nhất `host:cust-edge`, mọi provenance `agent:tenant-replay-01_cust-edge`
   (không có `agent:unknown`, không có host của tenant khác).
5. **Cross-tenant isolation — VERIFIED_RUNTIME**: `redis-cli HGET omni:aoip:system_model:staging-sim
   facts` vẫn nguyên 78 facts / 3 host (`cust-edge`, `cust-app`, `cust-db`) — không bị agent mới ghi
   đè hay trộn lẫn. Ở tầng API: `GET /onboarding/unknowns?tenant_id=staging-sim` gọi bằng API key
   của `tenant-replay-01` → HTTP 200 nhưng **body trả về `tenant_id: tenant-replay-01`** (281
   unknowns của chính tenant gọi, không phải của staging-sim) — `resolve_scope()`
   (`src/gateway/tenant_context.py:29`) cố tình bỏ qua `override_tid` cho caller non-admin, chỉ dùng
   `ctx.tenant_id` từ key. Ban đầu nghi ngờ đây là lỗ hổng cách ly (chỉ check HTTP status code) —
   sau khi đọc body mới xác nhận KHÔNG phải bug, chỉ là hành vi cố ý (silent-scope-override, không
   phải 403) — **lưu ý UX**: caller không biết mình đang bị scope lại âm thầm, đáng cân nhắc trả
   403 hoặc warning thay vì âm thầm đổi scope ở iteration UX sau, nhưng không phải data-loss/security
   defect.
6. Operator-visible qua API thật: `GET /onboarding/unknowns?tenant_id=tenant-replay-01` (Bearer
   đúng key) → Unknown thật (`svc:systemd-networkd` facet `owner`, `svc:omni-remote-agent` facet
   `upstream`, …); `GET /onboarding/competency?tenant_id=tenant-replay-01&entity_type=host&entity_id=host:cust-edge`
   → `identity: VERIFIED`, evidence_refs trỏ `agent:tenant-replay-01_cust-edge` +
   `discovery:port_scan/process_list` thật.

**Chưa DONE** (để lại cho iteration sau, không mở rộng trong iteration này):
- Chỉ 1/1 host cho tenant-replay-01 (dùng chung VM `cust-edge` với staging-sim qua 2 agent song
  song) — chưa test multi-host thật cho tenant thứ hai.
- Chưa viết automated test (unit/integration) cho kịch bản "2 agent, 2 tenant, cùng 1 VM" — mới chỉ
  runtime proof thủ công qua `orb -m` + `redis-cli` + `curl` trong pod.
- Chưa cập nhật `scripts/provision_fresh_tenant.py` / `remote_agent_provisioning.py` để tự động hoá
  bước "thêm API key vào `omni-gateway-secret`" — hiện làm tay qua `kubectl` patch, chưa canonical.
- UX gap `resolve_scope()` silent override (mục 5 ở trên) chưa fix, chỉ ghi nhận.

Verify: `orb -m cust-edge sudo systemctl status omni-remote-agent-replay01.service` (active running);
`kubectl exec pod/redis-0 -- redis-cli HGET omni:aoip:system_model:tenant-replay-01 facts` (41
facts, đúng agent_id); `kubectl exec deploy/omni-gateway -- curl .../onboarding/unknowns?tenant_id=tenant-replay-01`
(200, Unknown thật).

## Iteration 10 — Canonicalize tenant API-key provisioning step (2026-07-02)

**Bottleneck đã fix**: iteration 9 mục "Chưa DONE" #3 — bước "thêm API key vào
`omni-gateway-secret`" trước đây làm tay qua `kubectl patch`, không có script canonical, không
idempotent, dễ lặp lỗi khi provision tenant tiếp theo.

**Fix**: `scripts/add_tenant_api_key.sh <tenant_id> [api_key]` — đọc giá trị hiện tại của
`OMNI_TENANT_APIKEYS` trong secret `omni-gateway-secret`, no-op nếu `tenant_id` đã tồn tại (in ra
key hiện có), sinh key mới bằng `openssl rand -hex 32` nếu không truyền, patch secret, rolling
restart + `rollout status` gateway.

**Runtime proof (VERIFIED_RUNTIME, chạy trực tiếp trên cluster lab thật, không chỉ code/test)**:
1. No-op path: chạy với `tenant-replay-01` (đã tồn tại từ iteration 9) → in đúng key hiện có, không
   patch, không restart.
2. Mutation path: chạy với tenant tạm `tenant-scripttest-01` → sinh key mới, patch secret thành
   công, `omni-gateway` rollout restart xanh (`successfully rolled out`).
3. Idempotency re-run: chạy lại lệnh giống hệt cho `tenant-scripttest-01` → no-op, trả đúng key vừa
   sinh ở bước 2 (không tạo entry trùng, không sinh key thứ hai).
4. Dọn dẹp: revert `OMNI_TENANT_APIKEYS` về đúng giá trị gốc (3 tenant: default/staging-sim/
   tenant-replay-01), rolling restart lại, verify secret content khớp nguyên trạng.
5. Gateway health sau toàn bộ chu trình: `kubectl get --raw
   /api/v1/namespaces/multi-agent/services/omni-gateway:80/proxy/healthz` → `{"status":"ok",
   "rate_limit_tps":1000}`; `printenv OMNI_TENANT_APIKEYS` trong pod mới xác nhận chứa
   `tenant-replay-01`.
6. Test suite liên quan: `.venv/bin/python -m pytest tests/ -q -k "onboarding or gateway_api or
   tenant"` → 146 passed.

**Chưa DONE**: script chưa được gọi tự động từ `scripts/provision_fresh_tenant.py` (vẫn là 2 bước
tách biệt: tạo tenant Postgres + thêm API key) — canonical hoá thành 1 lệnh duy nhất là candidate
cho iteration sau. UX gap `resolve_scope()` silent override vẫn chưa fix (không phải bottleneck của
iteration này).

Verify: `bash scripts/add_tenant_api_key.sh tenant-replay-01` (no-op, in key hiện có);
`git show HEAD -- scripts/add_tenant_api_key.sh`.

## Iteration 11 — Wire API-key provisioning into single-command tenant provisioning (2026-07-02)

**Bottleneck đã fix**: iteration 10 mục "Chưa DONE" — `scripts/provision_fresh_tenant.py` (Postgres
tenant row) và `scripts/add_tenant_api_key.sh` (gateway secret) vẫn là 2 lệnh tách biệt, dễ quên bước
thứ hai khi provision tenant mới.

**Fix**: `provision_fresh_tenant.py` gọi `provision_api_key()` (subprocess `bash
scripts/add_tenant_api_key.sh <tenant_id>`) ngay sau khi Postgres row provision thành công. Thêm cờ
`--skip-api-key` cho trường hợp caller muốn tự quản lý gateway secret riêng.

**Runtime proof (VERIFIED_RUNTIME, chạy trực tiếp trên cluster lab thật)**:
1. Port-forward `omni-postgres:5432` → chạy `OMNI_ADMIN_PG_DSN=... PYTHONPATH=src python
   scripts/provision_fresh_tenant.py --tenant-id tenant-wiretest-01 --display-name "Wire Test 01"`
   → cả 2 bước chạy trong 1 lệnh: log `provisioned tenant=tenant-wiretest-01` rồi
   `[add_tenant_api_key] generated new key ... secret updated ... successfully rolled out`.
2. Idempotency re-run: chạy lại lệnh giống hệt → Postgres `create_tenant(idempotent=True)` no-op,
   `add_tenant_api_key.sh` in đúng key cũ, không patch/không restart lần 2.
3. Gateway health sau mutation: port-forward `svc/omni-gateway:80` → `curl .../healthz` →
   `{"status":"ok","rate_limit_tps":1000}`.
4. Dọn dẹp: revert `OMNI_TENANT_APIKEYS` về đúng 3-tenant gốc (default/staging-sim/
   tenant-replay-01) + rolling restart + verify secret content khớp nguyên trạng + healthz lại `ok`;
   `DELETE FROM omni_admin.tenant WHERE tenant_id='tenant-wiretest-01'` → xác nhận
   `SELECT tenant_id FROM omni_admin.tenant` chỉ còn 3 tenant gốc.
5. Test suite liên quan: `.venv/bin/python -m pytest tests/ -q -k "onboarding or gateway_api or
   tenant" --ignore=tests/integration` → 146 passed (không đổi so với iteration 10 — không thêm test
   mới, chỉ verify không regress).

**Chưa DONE**: chưa có automated test (pytest, không chỉ live-cluster manual run) cho
`provision_fresh_tenant.py` gọi `provision_api_key()` — vẫn là runtime proof thủ công, chưa CI-safe.
Multi-host cho `tenant-replay-01`, `resolve_scope()` UX gap, "2 agents/2 tenants" test coverage vẫn
mở từ iteration 9.

Verify: `git show HEAD -- scripts/provision_fresh_tenant.py`; `grep -n provision_api_key
scripts/provision_fresh_tenant.py`.

## Iteration 14 — tenant-replay-01 multi-host: real second Agent on cust-app (2026-07-02)

**Bottleneck đã fix**: iteration 9's last open leftover — `tenant-replay-01` chỉ có 1/1 host
(`cust-edge`), chưa chứng minh một Twin gộp fact từ nhiều host phân biệt cho cùng một tenant (khác
với iteration 9's "2 tenant share 1 host" scenario).

**Fix (runtime, không phải chỉ code)**: cài một Remote Agent instance thứ hai cho `tenant-replay-01`
trên VM `cust-app` (bên cạnh agent `staging-sim` sẵn có trên cùng VM đó), theo đúng pattern đã dùng
cho `cust-edge` ở iteration 9: `/opt/omni-remote-agent-replay01/` (venv symlink tới venv của
`staging-sim` để tiết kiệm ổ đĩa, code `remote_agent` copy riêng), `run.env` với
`OMNI_AGENT_ID=tenant-replay-01_cust-app`, `OMNI_AGENT_TENANT_ID=tenant-replay-01`,
`OMNI_AGENT_HOSTNAME=cust-app`, dùng lại API key đã có sẵn trong `OMNI_TENANT_APIKEYS` (không cần
provision key mới). Systemd unit `omni-remote-agent-replay01.service` enable+start.

**VERIFIED_RUNTIME**: log agent cho thấy register (200 OK) → profile (200 OK) → evidence (200 OK,
enqueued=5) trong vòng ~2s sau start. Redis `omni:aoip:system_model:tenant-replay-01` revision tăng
54→66; `facts` field (HGET) parse ra 2 host phân biệt: `{'cust-app', 'cust-edge'}` (trước đó chỉ
`{'cust-edge'}`). Isolation cross-check: `omni:aoip:system_model:staging-sim` (cùng chia sẻ VM
`cust-app`) không đổi shape — vẫn `{'cust-edge', 'cust-db', 'cust-app'}`/76 fact, không bị agent mới
ghi đè hay trộn lẫn. Operator-facing proof: `GET /onboarding/competency?entity_type=host&entity_id=
host:cust-app` (Bearer token của `tenant-replay-01`) trả về facet `identity`/`runtime_state` VERIFIED
với `evidence_refs` trỏ `agent:tenant-replay-01_cust-app` — API thật, không phải chỉ Redis key tồn
tại.

**VERIFIED_TEST**: thêm `tests/test_onboarding_pipeline.py::TestOneTenantTwoHosts` (2 test mới) —
chạy `accumulate_discovery_evidence()` thật với 2 envelope cùng `tenant_id=tenant-replay-01` nhưng
khác `namespace`/`agent_id` (`cust-edge` vs `cust-app`), assert Twin merge cả 2 host vào cùng 1
`system_model` (`revision=2`, `hosts == {"host:cust-edge", "host:cust-app"}`), và provenance mỗi
host chỉ tag đúng agent_id của chính host đó. `.venv/bin/python -m pytest
tests/test_onboarding_pipeline.py -q` → 31 passed (was 29). Regression `-k "onboarding or
gateway_api or tenant or provision" --ignore=tests/integration -q` → 159 passed (was 157, no
regression). `OMNI_AUTO_EXECUTE_ENABLED=false` reconfirmed on `omni-fullstack` post-mutation (agent
install is a read-only-evidence VM change, not a K8s mutation — no executor/CRAT path involved).

**Chưa DONE**: `cust-db` chưa có agent cho `tenant-replay-01` (2/3 host phủ, giống pattern
`staging-sim` ở iteration 2 trước khi cust-app được thêm) — không phải bug, chỉ là scope chưa mở
rộng; không cần thiết cho việc chứng minh multi-host capability đã hoạt động. Với mục này đóng,
toàn bộ leftover list của iteration 9 nay đã closed.

Verify: `.venv/bin/python -m pytest tests/test_onboarding_pipeline.py -q -k OneTenantTwoHosts`;
`orb -m cust-app sudo systemctl status omni-remote-agent-replay01.service`.

## Iteration 15 — Human Claim (Unknown→Question→Claim) VERIFIED_RUNTIME, readiness-gate disconnect found (2026-07-02)

**Bottleneck đã fix**: golden-journey link "Unknown → Question → Human Claim → Verification" had
code (`aoip.question_lifecycle`, `POST /onboarding/questions/{id}/answer`) and unit tests
(`tests/test_aoip_question_lifecycle.py`, `tests/test_gateway_onboarding_competency_routes.py`,
19 tests) but had never been exercised against the real running cluster — `PRODUCT_PROOF.md` row 28
still said "chưa kiểm trong iteration 1".

**VERIFIED_RUNTIME**: via `kubectl port-forward svc/omni-gateway 18090:80`, fetched real PENDING
questions for tenant `staging-sim` (`GET /onboarding/questions`) — found
`bdb9bb5e66be555d1fd3dd80` (`svc:nginx`, facet `business_capability`, PENDING). Answered it as a
human operator would: `POST /onboarding/questions/bdb9bb5e66be555d1fd3dd80/answer` with a real
`value` → `200 OK`, `answer_id=a8ddaa6bd49e2f83b9cb`, question status flips PENDING→ANSWERED
(re-fetched and confirmed). `GET /onboarding/competency?entity_type=service&entity_id=svc:nginx`
afterward shows facet `business_capability: state=CLAIMED, value="nginx serves the customer-facing
reverse proxy for cust-edge", evidence_refs=["human:iter15-productizer",
"question:bdb9bb5e66be555d1fd3dd80"], source_types=["human"]` — correctly stays `CLAIMED` (not
auto-promoted to `VERIFIED`) since there is no matching machine `Fact` for `business_capability`;
this confirms `competency_matrix`'s documented "Claim only promotes to VERIFIED via a matching
machine Fact" contract holds on the real Twin, not just in unit tests.

**Gap found (not fixed this iteration)**: `compute_readiness()` /
`compute_business_flow_pct()` (`src/pkg/onboarding/discovery_doc.py:198-203`) — the
`business_flow_confirmed_pct` component of the `UnderstandingComplete`/readiness gate is computed
from a *different, disconnected* mechanism: a `service_topology` probe's `services[].described`
boolean, which today is only ever set by a machine-parsed comment field in
`src/remote_agent/collectors/discovery_evidence.py:122` (`parts[4]`), never by a human `Claim`
answered through `/onboarding/questions/{id}/answer`. Practically this means answering every open
Question for a tenant does **not** move that tenant's `readiness_flag` toward `true` — the
Unknown→Question→Claim loop and the readiness/`UnderstandingComplete` gate are two parallel systems
that don't talk to each other. Confirmed live: `staging-sim`'s `readiness_flag` was `false` before
and after this iteration's answer (`compute_business_flow_pct` is unaffected by
`competency_matrix`/`question_lifecycle` state). This is the next real bottleneck for Phase 6/7 of
the golden journey — not addressed here because it requires a design decision (does readiness read
from `competency_matrix` coverage instead of/in addition to `service_topology.described`?), which is
out of scope for a single vertical slice without more research into who else depends on
`compute_business_flow_pct`'s current contract.

Verify: `tests/test_aoip_question_lifecycle.py`, `tests/test_gateway_onboarding_competency_routes.py`
→ 19 passed (no code change this iteration, runtime-verification only). No K8s mutation —
`OMNI_AUTO_EXECUTE_ENABLED=false` unchanged.

## Iteration 16 — Handover-doc upload (A8) VERIFIED_RUNTIME (2026-07-02)

**Bottleneck đã fix**: `POST /onboarding/handover-doc` (`src/gateway/routes/onboarding.py:111`) had
code + a data-residency design claim in its docstring ("content is hashed ... never persisted") but,
per iteration 15's leftover list, had never been exercised against the real cluster — only
`tests/test_onboarding_pipeline.py` unit-tested it.

**VERIFIED_RUNTIME**: via the existing `kubectl port-forward svc/omni-gateway 18080:80`, captured
`staging-sim`'s diagram version before (`GET /onboarding/diagram` → `version=6747`), then
`POST /onboarding/handover-doc` with `{"filename": "iter16-runbook.md", "content": "<124-char
runbook text>", "tenant_id": "staging-sim"}` using the real tenant bearer key from
`omni-gateway-secret`/`OMNI_TENANT_APIKEYS` → `200 OK`, `{"status":"ok","diagram_version":6752,...}`
— diagram version advanced 6747→6752, proving `dd.accumulate_probe_fact()` +
`dd.regenerate_diagrams()` ran against the real Redis-backed pipeline, not just returned a canned
response. Re-fetched `GET /onboarding/doc?tenant_id=staging-sim` afterward: the accumulated doc now
has a `doc_snapshot` key containing `{"documents":[{"path":"iter16-runbook.md",
"content_hash":"5429b992...", "content_length":124}]}` — **no raw content field**, confirming the
`INV_DATA_RESIDENCY` claim in the route's docstring holds on the real pipeline (only path/hash/length
persisted on the Omni side, matching the same contract already verified for discovery-evidence docs
via `document_store.ingest_customer_knowledge()`).

Readiness after the call was unchanged (`business_flow_confirmed_pct=100.0`,
`readiness_flag=false`) — expected, since handover-doc accumulation feeds `service_topology`-style
facts, not `competency_matrix`, and iteration 15 already documented that `readiness_flag` has its own
unrelated blocker (`open_questions_over_threshold`/gate design gap).

Verify: `pytest tests/test_onboarding_pipeline.py -q -k handover` → 3 passed (no code change this
iteration, runtime-verification only). No K8s mutation — `kubectl exec deploy/omni-gateway --
printenv OMNI_AUTO_EXECUTE_ENABLED` → `false`, confirmed unchanged.

## Iteration 20 — Answer-question trên portal (Phase-2, write-action đầu tiên) VERIFIED_RUNTIME (2026-07-03)

**Mục tiêu**: đóng loop Unknown→Question→Claim ngay trên official portal — operator trả lời
PENDING question từ trang `/understanding`, không cần curl Bearer key. Backend
`POST /onboarding/questions/{id}/answer` đã runtime-verified iter 15, iteration này KHÔNG đổi
Python — chỉ thêm lớp portal.

**Deliverables**:
- `ui/app/api/onboarding/answer/route.ts` (mới) — proxy POST duy nhất của portal tới gateway
  `/onboarding/questions/{id}/answer`; validate question_id pattern + answered_by (≤120) +
  value (≤500) trước khi forward; `source_channel="portal"`; honest error (không mock fallback),
  forward nguyên status/detail từ gateway.
- `ui/app/understanding/page.tsx` — nút "Answer" trên mỗi PENDING question mở form inline
  (answered_by + value), submit → refresh data; hiển thị ANSWERED optimistic sau khi 200; lỗi
  render qua `SectionError`; reset state khi đổi tenant.

**Tests**: full suite `pytest tests/ -q --ignore=tests/integration` → **5968 passed, 0 failed**
(cả flake đã biết cũng pass lần này). `cd ui && npm run build` xanh, route
`/api/onboarding/answer` có trong build output.

**Runtime proof (VERIFIED_RUNTIME, cluster lab thật)**:
1. Rebuild `omni-ui:latest` (`4cdb63f6e68a…`), rollout restart — pod chạy đúng digest mới
   (xác minh `imageID` trên pod).
2. Login NextAuth thật qua port-forward + Host `omni.ai-agent.local` (csrf → callback/credentials
   → session role=admin). 341 PENDING questions của `staging-sim` tại thời điểm proof.
3. `POST /api/onboarding/answer` (cookie session, question `a96324a653fe6491b3be9fec` —
   facet `sla` của `svc:systemd-udevd`) → 200
   `{"status":"ok","answer":{"answer_id":"5abc1da3499876efd4bb","source_channel":"portal",…}}`.
4. Re-fetch qua aggregate API → question đó `status=ANSWERED`, `answer_id` khớp — state
   transition thật trong Redis, không phải optimistic UI.
5. Unauthenticated POST → 401 (middleware); body có question_id không hợp lệ → 400
   `{"error":"invalid question_id"}` (validate ở proxy, không đụng gateway).
6. `GET /understanding?tenant=staging-sim` → 200; `OMNI_AUTO_EXECUTE_ENABLED=false` reconfirmed.

**Chưa DONE (slice sau)**: Claim→VERIFIED promotion cần Fact máy khớp (đúng thiết kế, không phải
gap); Mermaid diagram render; Playwright E2E cho `/understanding`.

## Iteration 19 — Operator Understanding surface (Phase-2 Golden Journey Read-only, slice 1) VERIFIED_RUNTIME (2026-07-03)

**Bottleneck đã fix**: Known Broken Link #1 + #4 — Twin/Competency/Unknowns chỉ có API, operator
phải biết endpoint + tự đoán `entity_id` format nội bộ (`host:cust-app`). Chưa có bất kỳ UI nào
trên official portal cho bước "Understanding Ready" của Golden Journey.

**Deliverables**:
- Gateway `GET /onboarding/entities` (mới, `src/gateway/routes/onboarding.py`) — entity index của
  System Twin (hosts/services + revision) từ `load_system_model`/`known_nodes`; UI dùng danh sách
  này thay vì bắt operator đoán `entity_id`. 3 test mới (TDD RED→GREEN) trong
  `tests/test_gateway_onboarding_competency_routes.py` (empty twin, grouping host/svc, tenant
  isolation) → 7 passed.
- UI trang `/understanding` (mới, `ui/app/understanding/page.tsx`) — readiness card, entity list
  (click → Competency Matrix facet table với state badge VERIFIED/CLAIMED/OBSERVED/CONTRADICTED/…,
  confidence, evidence_refs), Open Unknowns (severity, link tới entity), Questions (PENDING/
  ANSWERED). TenantSelector + honest per-section error (không mock fallback). Sidebar link
  "Understanding" ở navOps/navFull/navPortal; hoạt động ở cả 2 realm (không thêm vào redirect
  prefix của middleware, giống `/pipeline`).
- Next proxy routes (mới): `ui/app/api/onboarding/understanding/route.ts` (aggregate song song
  entities+unknowns+questions+readiness, mỗi section trả `{data,error}` trung thực) và
  `ui/app/api/onboarding/competency/route.ts` (passthrough per-entity).
- Fix phụ: root `ui/tsconfig.json` exclude `apps`/`packages` (workspace app riêng có tsconfig/
  Dockerfile riêng làm root `next build` fail type-check từ trước — pre-existing latent break,
  ghi TECH_DEBT_BACKLOG #14).

**Tests**: full suite `pytest tests/ -q --ignore=tests/integration` → **5967 passed, 1 failed**
(chỉ flake đã biết `test_register_then_real_system_metrics_emitted_through_real_pipeline`, đã ghi
trong `AUTONOMOUS_LOOP_STATE.json` từ trước). `ui: npm run build` xanh, route `/understanding` xuất
hiện trong build manifest.

**Runtime proof (VERIFIED_RUNTIME, cluster lab thật)**:
1. Rebuild `omni-gateway:latest` (`aa24b92ad3bf…`) + `omni-ui:latest` (`b0c85bbdd6d7…`), rollout
   restart cả hai — successfully rolled out; `/readyz` → 200 redis+postgres ok;
   `OMNI_AUTO_EXECUTE_ENABLED=false` reconfirmed.
2. Gateway: `GET /onboarding/entities?tenant_id=staging-sim` (Bearer key thật, trong pod) →
   revision 2793, hosts đủ 3/3 (`cust-app/cust-db/cust-edge`), 7 services thật (`svc:mariadbd`,
   `svc:nginx`, `svc:redis-server`, …).
3. UI end-to-end **có auth thật** (không bypass middleware): login NextAuth credentials qua
   port-forward + Host `omni.ai-agent.local` (cookie domain `.ai-agent.local`), rồi:
   `GET /api/onboarding/understanding?tenant_id=staging-sim` → source=gateway, entities 3 host/7 svc
   (revision 2814 — tăng theo thời gian thực), 352 unknowns, 336 questions, readiness_flag=true;
   `GET /api/onboarding/competency?entity_type=host&entity_id=host:cust-app` → facet thật
   (`identity: VERIFIED conf=0.85`, `process: CONTRADICTED`, coverage 50%, evidence_refs trỏ
   `agent:staging-sim_cust-app` + `discovery:port_scan:ra-…`); `GET /understanding?tenant=staging-sim`
   → HTTP 200, HTML chứa "System Understanding".
4. Unauthenticated call → 401 (middleware giữ nguyên contract).

**Chưa DONE (không mở rộng trong iteration này)**: nút "Answer" cho PENDING question trên UI (API
`POST /onboarding/questions/{id}/answer` đã có, iteration 15 runtime-verified — đây là write-action,
để slice sau của Phase 2); diagram Mermaid chưa render trên trang; Playwright E2E cho trang mới.

## Iteration 18 — Phase-1 Product & Architecture Contract Freeze VERIFIED_RUNTIME (2026-07-03)

**Mục tiêu**: mở màn production productization plan — chốt Product Contract + canonical command
protocol trước khi mở thêm AI capability (master plan Phase 1). Iteration 17 xác nhận đã đóng
sạch (ledger/state/PROOF đồng bộ, kill-switch false xác minh trên pod) trước khi bắt đầu.

**Deliverables**:
- `docs/product/PRODUCT_CONTRACT.md` (mới) — target customer, supported platforms, Golden Journey
  chính thức, catalog 3 remediation đầu (RestartSystemdService/RestartKubernetesWorkload/
  RollbackKubernetesDeployment), 5 hard-zero SLO, autonomy tier gates, data boundary, non-goals,
  pilot acceptance criteria.
- `docs/architecture/ADR-002-command-protocol.md` (mới) — **phát hiện quan trọng**: hướng hợp nhất
  ghi ở ADR-001 §5 ("gateway import `DurableCommandChannel`") là sai chiều. Đọc kỹ cả hai file cho
  thấy `gateway/routes/agent_runtime.py` đã VƯỢT bản aoip về an toàn (atomic Lua claim, fencing
  token, delivery_attempt, record_version, visibility heartbeat) trong khi `DurableCommandChannel`
  thiếu fencing và có race non-atomic ở poll, chỉ còn dùng trong tests/demo. Canonical = HTTP
  contract + state machine của `agent_runtime.py`; `DurableCommandChannel` = legacy có sunset
  criteria (Phase-3 durable Control Plane). ADR-001 được đánh dấu superseded ở §5.
- `src/aoip/protocol/__init__.py` (mới) — nguồn chân lý DUY NHẤT cho state vocabulary
  (9 states, TERMINAL, PROGRESS, `is_legal_transition()`, PROTOCOL_VERSION=1). Trước đây bộ hằng
  số này bị chép tay ở 3 nơi (agent_runtime.py, delivery.py, và hardcode TRONG Lua `_CLAIM_SCRIPT`).
  Cả `agent_runtime.py` lẫn `delivery.py` giờ import từ đây (refactor import-only, hành vi không
  đổi); Lua không import được → `tests/test_aoip_protocol_contract.py` (13 test mới) parse Lua
  source và fail nếu bảng TERMINAL drift, kèm transition-invariant tests (terminal absorbing,
  redelivery legality, no-backward-progress).
- `requirements.lock` (mới) — pip freeze snapshot Python 3.13.5; Dockerfile chưa wire (ghi
  TECH_DEBT_BACKLOG #13).

**Tests**: `pytest tests/ -q --ignore=tests/integration` → **5965 passed, 0 failed** (13 test mới;
flake đã biết cũng pass lần chạy này).

**Runtime proof**: rebuild `omni-gateway:latest` (`f9ccdf1fe277…`) + `multi-agent-system:latest`
(`bfa8fe4b053f…`), rollout restart omni-gateway/omni-fullstack/omni-onboarding — tất cả rolled out.
`kubectl exec` xác minh trong pod thật: gateway `agent_runtime.TERMINAL is protocol.TERMINAL_STATES
== True`, fullstack `delivery.TERMINAL_STATES is protocol.TERMINAL_STATES == True`. `/readyz` →
200 `{"redis":"ok","postgres":"ok"}` qua port-forward. `OMNI_AUTO_EXECUTE_ENABLED=false` xác minh
lại sau deploy.

**Next bottleneck**: Golden Journey Read-only (master plan Phase 2) — hành trình create-tenant →
export-audit qua official API/portal, không Redis/DB manual. Ứng viên đầu: operator portal UI cho
competency/unknowns (hiện API-only, carry-over từ iteration 17).

## Iteration 17 — readiness-gate/competency wiring VERIFIED_RUNTIME (2026-07-02)

**Bottleneck đã fix**: the design-decision gap found in iteration 15 —
`compute_business_flow_pct()` (`src/pkg/onboarding/discovery_doc.py`) only read
`service_topology.services[].described` (machine-set, from the agent-parsed systemd comment probe).
It never read `competency_matrix`/Human Claims, so answering every open `Question` for a tenant
(Slice O2B) did not move `business_flow_confirmed_pct` or `readiness_flag` — the highest-value
remaining disconnect in the golden journey `Unknown → Question → Human Claim → ... →
UnderstandingComplete`.

**Design decision**: a service now counts as "confirmed" for `business_flow_confirmed_pct` if
EITHER the discovery-doc `described` flag is true (existing machine-set path, unchanged) OR the
Entity Competency Matrix (`aoip.competency_matrix.build_entity_competency()`) reports a
CLAIMED/VERIFIED `business_capability` facet for that service (i.e. a Human Claim was answered via
`POST /onboarding/questions/{id}/answer`, the same O2B flow iteration 15 runtime-verified). No LLM
involved — `build_entity_competency()` is the existing pure/deterministic projection over
`SystemModel` + claims + contradictions (already used by `GET /onboarding/competency`).

**Code change**: `compute_business_flow_pct(doc)` (sync) → `compute_business_flow_pct(redis,
tenant_id, doc)` (async) in `src/pkg/onboarding/discovery_doc.py`. Confirmed via grep the only
caller was `compute_readiness()`, which was already `async`/already awaited by both callers
(`src/workers/onboarding_pipeline.py::recompute_readiness`, `src/gateway/routes/onboarding.py`) —
not a breaking change for any other code path.

**Tests**: new `tests/test_onboarding_pipeline.py::TestReadinessThresholds::
test_answered_human_claim_counts_toward_business_flow_pct` — a service with no discovery-doc
description but an answered Claim reaches `business_flow_confirmed_pct == 100.0`.
`pytest tests/test_onboarding_pipeline.py -q` → 32 passed (was 31). Regression `-k "onboarding or
gateway_api or tenant or provision or competency or claim" --ignore=tests/integration` → 203
passed. Full suite `pytest tests/ -q --ignore=tests/integration` → 5956 passed, 1 pre-existing known
flake (`test_register_then_real_system_metrics_emitted_through_real_pipeline`, unrelated to this
change — documented in `AUTONOMOUS_LOOP_STATE.json` resume_checks before this iteration started).

**Build+deploy**: `make docker-worker` + `make docker-gateway` rebuilt `multi-agent-system:latest`
and `omni-gateway:latest`, then `kubectl rollout restart` on `omni-fullstack`, `omni-onboarding`,
`omni-gateway` (all three import `discovery_doc.py` — full/onboarding worker roles and the gateway's
manual routes). All three rolled out successfully. Confirmed the new signature is live in the
running pod: `kubectl exec deploy/omni-gateway -- python -c "import inspect; from pkg.onboarding
import discovery_doc as dd; print(inspect.signature(dd.compute_business_flow_pct))"` →
`(redis: 'Any', tenant_id: 'str', doc: 'dict[str, Any]') -> 'float'`.

**VERIFIED_RUNTIME proof**: both real lab tenants (`staging-sim`, `tenant-replay-01`) already have
100% of their real services `described` (systemd comment probe covers every process), so the new
claim-based branch has no observable *real* undescribed service to flip today — expected, not a
gap. Proved the wiring end-to-end against the live pod's real Redis instead: `kubectl exec
deploy/omni-gateway` running a Python snippet that calls the exact same deployed
`dd.accumulate_probe_fact`/`dd.compute_readiness`/`put_claim` functions (no test doubles) against a
disposable scratch tenant `iter17-readiness-proof` — `business_flow_confirmed_pct` moved
`0.0 → 100.0` and `readiness_flag` flipped `False → True` purely from an answered Claim (no
`described` flag ever set true). Scratch tenant's Redis keys (`omni:onboarding:doc:*`,
`omni:aoip:claims:*`) deleted immediately after — confirmed both keys `exists() == 0` post-cleanup.
Re-checked both real tenants' `/onboarding/readiness` unaffected by the deploy
(`staging-sim`: unchanged at `business_flow_confirmed_pct=100.0`; `tenant-replay-01`: unchanged at
`readiness_flag=true`). `kubectl exec deploy/omni-fullstack -- printenv OMNI_AUTO_EXECUTE_ENABLED` →
`false`, confirmed unchanged throughout.

## Iteration 13 — "2 agents/2 tenants on 1 VM" test coverage + resolve_scope() closed (2026-07-02)

**Bottleneck đã fix**: iteration 9's leftover — the cross-tenant isolation proof for two Remote
Agent instances (`staging-sim`, `tenant-replay-01`) both running on VM `cust-edge` only had
live-cluster manual verification (Twin fact counts inspected by hand via `redis-cli`), no automated
regression test locking the behavior in.

**Fix**: `tests/test_onboarding_pipeline.py::TestTwoAgentsTwoTenantsOneVM` (2 test mới) — chạy thẳng
qua `workers.onboarding_pipeline.accumulate_discovery_evidence()` (entrypoint thật của Kafka
discovery-evidence worker), với 2 evidence envelope cùng `namespace`/hostname (`cust-edge`) nhưng
khác `tenant_id`/`agent_id`. Assert: (1) mỗi Twin (`aoip.system_model_store`,
`omni:aoip:system_model:{tenant_id}`) chỉ chứa fact của chính tenant đó dù cùng subject
`host:cust-edge`; (2) legacy flat-doc accumulation (`pkg.onboarding.discovery_doc`) cũng cô lập theo
tenant; (3) Fact provenance không bao giờ lẫn `agent_id` của tenant khác. Root cause của việc cô lập
này là structural, không phải run-time may mắn: `system_model_store.MODEL_KEY =
"omni:aoip:system_model:{tenant_id}"` — key Redis khoá theo `tenant_id`, nên 2 tenant share hostname
vẫn fold vào 2 Twin độc lập hoàn toàn tách biệt về storage.

**Đã điều tra thêm (không cần fix)**: `resolve_scope()` non-admin silent-override từng bị ghi là "UX
gap chưa fix" ở iteration 9. Kiểm tra lại `tests/test_tenant_isolation.py::TestResolveScope::
test_non_admin_ignores_override` và `TestKpiTenantIsolation::test_non_admin_cannot_scope_override` —
cả hai đã khóa hành vi silent-ignore này làm contract chính thức, có test từ trước. Đổi sang trả về
403 sẽ là breaking change với một quyết định thiết kế đã chốt, không phải sửa bug. Đóng mục này là
"intentional, no action" thay vì "open gap" trong `current-priority.md`.

**VERIFIED_TEST**: `.venv/bin/python -m pytest tests/test_onboarding_pipeline.py -q` → 29 passed
(was 27). Regression `.venv/bin/python -m pytest -k "onboarding or gateway_api or tenant or
provision" --ignore=tests/integration -q` → 157 passed (was 155, no regression). Không có mutation
cluster thật trong iteration này (test-only) — `OMNI_AUTO_EXECUTE_ENABLED=false` reconfirmed qua
`kubectl exec deploy/omni-fullstack -- printenv`.

**Chưa DONE**: multi-host cho `tenant-replay-01` (vẫn 1/1 host) — item duy nhất còn mở từ iteration
9's leftovers list. Đây là quyết định cần trước khi coi Phase 6/7 của slice "Repeatable Tenant
Onboarding Baseline" là DONE hoàn toàn.

Verify: `.venv/bin/python -m pytest tests/test_onboarding_pipeline.py -q -k TwoAgentsTwoTenants`.

## Iteration 12 — pytest coverage for provision_api_key() (2026-07-02)

**Bottleneck đã fix**: iteration 11's "Chưa DONE" — `provision_api_key()` (subprocess wrapper
calling `add_tenant_api_key.sh`) only had live-cluster manual proof, no CI-safe automated test.

**Fix**: `tests/test_provision_fresh_tenant.py` (mới, 3 test) — mock `subprocess.run` to assert
`provision_api_key()` invokes `["bash", str(ADD_API_KEY_SCRIPT), tenant_id]` với `check=True` và
đúng `cwd` (project root); asserts `subprocess.CalledProcessError` propagates (no swallowed
failure); asserts `ADD_API_KEY_SCRIPT` resolves to a real file on disk. Scope: subprocess wiring
only — the real Postgres/gateway mutation path stays covered by the iteration 11 live-cluster
runtime proof, not re-tested here (would require live Postgres + gateway, out of scope for a unit
test).

**VERIFIED_TEST**: `.venv/bin/python -m pytest tests/test_provision_fresh_tenant.py -q` → 3 passed.
Regression check `.venv/bin/python -m pytest -k "onboarding or gateway_api or tenant or provision"
--ignore=tests/integration -q` → 155 passed (was 146 in iteration 11 baseline + these 3 new + other
already-existing tests newly matched by the broadened `provision` keyword).

**Chưa DONE**: multi-host cho `tenant-replay-01`, `resolve_scope()` UX gap, "2 agents/2 tenants" test
coverage vẫn mở từ iteration 9 — decide whether Phase 6/7 of the slice needs these before declaring
"Repeatable Tenant Onboarding Baseline" fully DONE.

Verify: `.venv/bin/python -m pytest tests/test_provision_fresh_tenant.py -q`.

## Iteration 7 — Fresh-tenant repeat-provisioning runtime proof (2026-07-02)

**Bottleneck đã fix (Phase 4 của slice "Repeatable Tenant Onboarding Baseline"):**

`scripts/provision_fresh_tenant.py` (MỚI) — canonical caller gọi thẳng
`AdminConfigRepo.create_tenant(idempotent=True)` qua `asyncpg` pool thật, tách biệt khỏi HTTP
`POST /autonomy/tenants` (gateway route cố ý giữ nguyên contract 409, không set `idempotent=True` —
xem mục 6 ở trên). Chạy THẬT 2 lần liên tiếp trên Postgres thật trong cluster (port-forward
`svc/omni-postgres`, tenant `tenant-replay-01`):
- Lần 1: tạo tenant, 1 row `omni_admin.tenant`, 1 row `omni_admin.config_change_log`
  (`actor=provisioning-tooling, action=create`).
- Lần 2 (idempotent=True): trả về result giống hệt, **không** raise, **không** row thêm — verify
  bằng `SELECT` trực tiếp: đúng 1 row tenant, đúng 1 audit event (`GROUP BY` count=1).

`VERIFIED_RUNTIME` cho riêng phần "create_tenant(idempotent=True) an toàn khi re-run trên Postgres
thật" — đây là bằng chứng runtime đầu tiên cho iteration 6 (trước đó chỉ `VERIFIED_TEST` với
FakePgPool). Chưa DONE toàn bộ Phase 4-7 của slice: `tenant-replay-01` mới tồn tại ở tầng
`omni_admin.tenant`, CHƯA có Agent provisioning + discovery + Twin + Competency thật cho tenant này
(đó là Phase 6, cần VM lab riêng — ngoài phạm vi iteration này).

Verify: `.venv/bin/python -m pytest tests/test_admin_config_store.py
tests/test_remote_agent_provisioning.py -q` → 29 passed. Runtime: `psql` query trực tiếp trên
`omni-postgres-0` qua port-forward, output đính kèm ledger.

## Iteration 5 — Safe evidence compaction + canonical provisioning module (2026-07-02)

**Bottleneck đã fix (Phase 1-3 của slice "Repeatable Tenant Onboarding Baseline"):**

1. `coerce_evidence_dict()` (`src/pkg/reasoning/schema.py`) — thay slicing thô `json.dumps(ef)[:2000]`
   (có thể cắt giữa JSON token → `json.loads()` lỗi → mất toàn bộ evidence, kể cả AOIP projection) bằng
   `_compact_extracted_fact()`: parse trước, shrink field lồng (string/list) theo budget giảm dần
   (500→200→80→20 ký tự/field), fallback drop nested collection nếu vẫn vượt — luôn trả JSON hợp lệ.
   Thêm field top-level `schema_version`, `truncated`, `original_size`, `content_hash` (SHA-256 của
   bản gốc chưa cắt) để downstream/audit biết evidence có bị compact hay không. `agent_id`/`hostname`
   vẫn được promote lên top-level TRƯỚC compaction (giữ nguyên logic iteration 4) nên luôn sống sót
   dù `extracted_fact` bị cắt. 9 test mới `tests/test_evidence_compaction.py` — dưới ngưỡng, đúng
   ngưỡng, vượt xa ngưỡng, unicode, nested sâu, `None` field, identity luôn còn, JSON list top-level.
2. **Canonical provisioning module** `scripts/lib/remote_agent_provisioning.py` (mới) — trích xuất
   f-string `run.env` viết tay trong `scripts/e2e_onboarding_full_flow.py` (TC-OB02, nguồn gốc gap
   cust-app thiếu `OMNI_REMOTE_DISCOVERY_ENABLED`) thành `AgentProvisioningSpec` + `render_run_env()`
   thuần Python, `discovery_enabled: bool = True` mặc định (không còn cách nào quên set flag này khi
   dùng module). Thêm `is_idempotent_rewrite()` (so sánh nội dung render, không rewrite/restart nếu
   giống hệt) và `effective_config_summary()` (non-secret, dùng để log lúc agent startup — chưa wire
   vào `remote_agent/agent.py` thật, chỉ có hàm sẵn sàng). `e2e_onboarding_full_flow.py` đã chuyển
   sang gọi module này thay vì f-string tay. 7 test mới `tests/test_remote_agent_provisioning.py`.

**Chưa làm (Phase 4-7, cần VM/cluster thật + thời gian dài hơn):** tạo tenant lab mới
(`tenant-replay-01`), provision qua canonical, chứng minh golden journey Tenant→Twin→Competency chạy
không sửa tay, chứng minh tenant isolation với `staging-sim`, chứng minh repeat-provisioning không phá
state, operator read-only proof flow. `AdminConfigRepo.create_tenant()` không idempotent (mục 6 ở
trên) sẽ chặn Phase 5 nếu không sửa trước.

Verify: `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` — xem log chạy trong
`docs/handoffs/CURRENT_SESSION.md`.

## Iteration 6 — omni-autonomous-productizer skill + tenant idempotency (2026-07-02)

Bootstrap skill `.claude/skills/omni-autonomous-productizer/` (Continuous Productization Loop
operator — Reality Map, evidence taxonomy, safety policy, quota/resume protocol, supervisor
fallback). Smoke-tested read-only (`reality_check.sh` chạy thật, xác nhận 3 VM lab Running +
`OMNI_AUTO_EXECUTE_ENABLED=false` trên `omni-fullstack`). Commit `5c76425`.

Iteration đầu tiên do skill này chọn (bottleneck #2 trong `references/current-priority.md`):
`AdminConfigRepo.create_tenant()` (`src/services/admin_config/repo.py:569-596`) thêm tham số opt-in
`idempotent: bool = False` — mặc định vẫn raise `ValueError` (giữ nguyên HTTP 409 contract của
`POST /autonomy/tenants`), nhưng khi `idempotent=True` (dành cho provisioning tooling nội bộ, ví dụ
fresh-tenant replay ở Phase 4-5 của slice "Repeatable Tenant Onboarding Baseline"), tenant đã tồn
tại được trả về nguyên trạng, không tạo dòng trùng, không tạo audit event trùng. Test mới
`test_create_tenant_idempotent_true_is_repeatable`
(`tests/test_admin_config_store.py`) verify: gọi 2 lần idempotent=True → cùng kết quả, đúng 1 row,
đúng 1 audit event. `VERIFIED_TEST` — CHƯA runtime-verify trên Postgres thật (chỉ FakePgPool), CHƯA
wire vào bất kỳ caller thật nào (đang chờ Phase 4 dùng).
