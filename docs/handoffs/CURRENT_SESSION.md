# Current Session Handoff

## Deliverable hiện tại
**PIVOT (user yêu cầu ngay trong phiên này, ưu tiên cao hơn migration bước 5)**: (a) làm thật sơ đồ
kiến trúc/API/service (không chỉ node cô lập — cần edge nối thật giữa các host), hiển thị đầy đủ
trên UI; (b) redesign toàn bộ `ui/packages/ui-kit` + `provider-portal` sang hướng **Technical/Ops
Console tối** (đã chốt với user qua AskUserQuestion, không đổi lại). Migration omni-ui→provider/
tenant (bước 1-4 DONE, bước 5 TẠM DỪNG) — xem phần "Migration omni-ui" bên dưới, tiếp tục sau khi
xong pivot này.

### Trạng thái pivot (3 phần)
1. **Redesign UI-kit — DONE, đã merge main, build xanh.** `ui/packages/ui-kit/src/styles.css`
   viết lại hoàn toàn: token surfaces 3 cấp, accent tín hiệu ok/warn/critical/info, mono cho
   số liệu + sans cho heading, density tăng, focus/hover có chủ đích. Áp dụng cho cả
   provider-portal và tenant-portal (dùng chung ui-kit).
2. **Backend: probe `connection_scan` + cross-host edge — DONE, đã merge main, verified.**
   Thêm probe `ss -tnp` (established, KHÔNG phải `-l`) vào
   `src/remote_agent/collectors/discovery_evidence.py::collect_connection_scan()`; đăng ký vào
   `SUPPORTED_PROBES` + chạy trong `src/remote_agent/agent.py`. `register_agent()`
   (`src/gateway/routes/agent_webhook.py`) giờ lưu `remote_ip` thật (từ `request.client.host`,
   không tin body) vào registry. `src/aoip/onboarding_projection.py::resolve_ip_to_host_map()`
   (mới) đọc `omni:remote_agent:registry:*`, lọc theo `tenant_id`, trả `{remote_ip: host}`.
   `project_facts(probe="connection_scan", ip_to_host=...)` sinh `Fact(connects_to)` CHỈ khi
   remote_ip resolve được ra host KHÁC trong cùng tenant — không đoán (IP ra Internet/DNS/NTP
   không resolve được → không sinh fact, đúng invariant "Never assume"). `connects_to` đã có sẵn
   trong `RELATIONAL_PREDICATES` (`src/aoip/objects.py`), không cần thêm.
   Verify: `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` → **5985 passed, 1
   failed** (fail = `test_register_then_real_system_metrics_emitted_through_real_pipeline`, đúng
   test env-dependent đã biết từ trước, KHÔNG phải regression — xem "Không được làm lại" cũ).
3. **Diagram UI toàn diện (host/service/API/edge thật) — DONE, merge main, verified.**
   Background agent chạy trong worktree CŨ (base `cf11f1f`, không thấy được các thay đổi
   uncommitted của bước 1-2 migration đã có trên main) → báo cáo sai là "provider-portal chưa có
   DiagramCard/mermaid-diagram.tsx/token Ops Console" và viết lại từ đầu bằng token cũ. ĐÃ KHÔNG
   copy đè phần frontend của agent — chỉ lấy phần backend (an toàn, độc lập, đúng):
   - `src/pkg/onboarding/discovery_doc.py`: thêm `render_system_topology_diagram(edges)` — duyệt
     `SystemModel.edges` (facts có predicate trong `RELATIONAL_PREDICATES`), vẽ node shape khác
     nhau theo loại entity (host=stadium `([...])`, svc=rect `[...]`, api=hexagon `{{...}}`,
     db=cylinder `[(...)]`, doc=rounded `(...)`), empty-state không crash khi chưa có edge.
     `render_all_diagrams()` giờ nhận thêm `edges` param, ghép thành section thứ 4 "system
     topology" (nối bằng `%%` comment, đúng format `splitDiagramText` đã parse từ trước).
     `regenerate_diagrams()` load `SystemModel` qua `load_system_model()` để lấy `model.edges`.
   - `tests/test_diagram_topology.py` (mới, TDD) — 5 test pass.
   - Verify: `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` → **5990 passed,
     1 failed** (cùng test env-dependent đã biết, không phải regression).
   Frontend: hạ tầng bước 1 đã tự động nhận section mới (không cần sửa gì thêm — `splitDiagramText`
   split theo `%%`, tự thêm section "system topology" khi backend trả về). Chỉ bổ sung 2 việc:
   - `ui/apps/provider-portal/app/understanding/understanding.css`: đổi hết hex cứng cũ
     (`#3fb950`, `#f0b429`, `#0e1620`, `#0b0f14`...) sang token Ops Console thật
     (`--color-ok`, `--color-warn`, `--surface-1`, `--space-*`, `--radius-*`, `--font-mono`) — file
     này sót lại từ bước 1, chưa được agent redesign chạm tới vì nó không phải `ui-kit/styles.css`.
     Thêm class `.aoip-diagram-legend*` mới cho legend.
   - `ui/apps/provider-portal/app/understanding/page.tsx`: thêm `DiagramLegend` (map node
     type→label: Host/Service/API/Database/Document, đồng bộ với
     `_TOPOLOGY_NODE_SHAPES`/`NODE_TYPE_PREFIX` bên backend — sửa 1 bên phải sửa bên kia) render
     phía trên các section trong `DiagramCard`.
   Verify: `cd ui/apps/provider-portal && npm run build` → xanh, 18 route.
   **Follow-up chưa làm** (agent phát hiện, chưa xử lý): provider BFF chưa có proxy route thật
   cho `/onboarding/diagram` — `lib/diagram.ts` hiện dùng `fetchGatewaySection` (đã đúng pattern
   BFF có sẵn từ bước 1, KHÔNG phải vấn đề — worktree cũ tưởng thiếu vì nó nhìn thấy code cũ).

## Quyết định kiến trúc mới (KHÔNG re-litigate)
User đã chốt: **"omni sẽ là provider, portal sẽ là tenant"** — tức là:
- `omni-ui` (K8s deployment `omni-ui`, host `portal.ai-agent.local` + `omni.ai-agent.local`)
  sẽ bị **retire** sau khi mọi route được port xong.
- Route/tính năng thuộc vai trò operator/SRE nội bộ → merge vào `ui/apps/provider-portal`
  (K8s `aoip-provider-portal`, host `provider.ai-agent.local`).
- Route/tính năng thuộc vai trò customer-facing/tenant → merge vào `ui/apps/tenant-portal`
  (K8s `aoip-tenant-portal`, host `tenant.ai-agent.local`, hiện gần trống).
- Không port thẳng code omni-ui (shadcn/`@/components/ui`, NextAuth cookie) — phải đổi sang
  design system provider-portal (`@aoip/ui-kit`) và auth pattern OIDC `resolveSession` sẵn có.

## Bảng mapping đầy đủ (18+ route omni-ui) và thứ tự thực hiện
Xem lại trong lịch sử hội thoại phiên này (planner agent đã sinh bảng); tóm tắt thứ tự:
1. **[DONE]** understanding/onboarding/incidents (đối chiếu + graph map).
2. **[DONE]** remote-agents → `agent-fleet` hoá ra là stub mồ côi (không trong `PROVIDER_NAV`,
   đã xóa), KHÔNG phải trùng lặp thật với `agents`. Đã thêm tenant filter thiếu vào `agents`
   (`AgentsTable.tsx` mới). Mutation features (install wizard, restart/enable/disable, log stream)
   CỐ Ý không port — provider-portal là read-only operational projection theo nav.ts.
3. **[DONE]** config/autonomy → policies: **CHẶN** (API `/autonomy/policy` global, không
   tenant-scoped; `nav.ts` đã cấm "policy-editor" domain từ 2026-07-01). deploy → deployments:
   **CHẶN** (bản gốc omni-ui chỉ là mock, không trigger K8s thật; cũng không tenant-scoped).
   siem → incidents: **ĐÃ PORT** (`/siem/overview` read-only, tenant-scoped; incidents flip
   `implemented: true`). workers → platform-health: **HOÃN** (port được kỹ thuật nhưng cần thêm
   route vào `PROVIDER_NAV`, là quyết định nav-architecture ngoài scope 1-capability/iteration).
   operator: **KHÔNG CÓ ĐÍCH PHÙ HỢP** — gộp 3 domain khác nhau (incident list/HITL decide/alert
   injection), cần slice riêng + review backend contract. Bonus: fix latent crash bug —
   `PROVIDER_NAV.find(...)!` throw runtime nếu href không có trong nav list (3 stub cũ bị lỗi này).
4. **[DONE]** admin/* (flags, hitl, kb, risk-class, tenants, tier, guide) — KHÔNG port thêm code
   nào (tất cả 7 trang bị chặn/hoãn có lý do cụ thể, xem báo cáo agent trong lịch sử phiên):
   - `flags`: tenant-scoped, API sống, nhưng chưa có nav slot → cần architect quyết định.
   - `hitl`: đối chiếu kỹ với `human-inbox` → **KHÔNG PHẢI cùng domain** (hitl = approve/reject
     action mutate qua Kafka `omni-hitl-decisions`; human-inbox = O2B Human Claim onboarding).
     Không gộp nhầm. Không có nav slot riêng.
   - `kb`: gateway `/kb` KHÔNG tenant-scoped → chặn.
   - `risk-class`: cùng họ autonomy write global-scope như policies → chặn cùng lý do.
   - `tenants`: **ứng viên mạnh nhất cho slice kế tiếp**, đích đúng là `customers` (không phải
     settings/users) — nhưng issue API key plaintext + revoke cần thiết kế bảo mật riêng, vượt
     phạm vi "port cơ học" của bước này.
   - `tier`: trang gốc bundle chung `TierControlPanel` (an toàn, tenant-scoped) VÀ `AutonomyPanel`
     (chính domain đã bị cấm ở policies) → chặn toàn trang vì bundle chung.
   - `guide`: vô hại nhưng không có nav slot "Help" → không port.
   - **Bonus fix (bug thật, không phải quyết định phạm vi)**: 5 trang `customers`, `onboarding`,
     `licenses`, `users`, `systems` dùng `PROVIDER_NAV.find(...)!` với href KHÔNG có trong
     `PROVIDER_NAV` → `undefined!.label` throw runtime nếu truy cập trực tiếp route (build không
     phát hiện vì toàn bộ là `ƒ` dynamic server-rendered). Đã sửa cả 5 thành static stub tự chứa,
     không phụ thuộc `PROVIDER_NAV.find()` nữa. `audit`/`missions` đã an toàn từ trước (có trong nav).
5. **[CHƯA BẮT ĐẦU — tạm dừng vì pivot]** Port mới: ledger, pipeline, trace, playbooks, kpi
   (pipeline+trace đi cùng vì chung hạ tầng SSE).
6. simulator — cuối, phải gate lab-only.
7. Tenant-portal subset (read-only): understanding/incidents/kpi/onboarding/remote-agents scoped theo membership.
8. api/* — port kèm mỗi route tương ứng, không làm rời.
9. Retire `omni-ui` deployment + ingress (`portal.`/`omni.ai-agent.local`).

## Đã hoàn thành (bước 1)
- `ui/apps/provider-portal/lib/gateway.ts`, `lib/readiness.ts`, `lib/diagram.ts` (mới) — fetcher
  server-side gọi gateway `GET /onboarding/readiness` và `GET /onboarding/diagram`.
- `ui/apps/provider-portal/components/mermaid-diagram.tsx` (mới) — port từ
  `ui/components/mermaid-diagram.tsx`, restyle theo `@aoip/ui-kit` (không dùng Tailwind gốc).
- `ui/apps/provider-portal/app/understanding/page.tsx` — thêm `ReadinessCard` (3 check +
  progress bar + target + badge Ready/Not ready) và `DiagramCard` (Mermaid entity graph, thay
  text phẳng "Open graph targets: a,b,c" cũ).
- `ui/apps/provider-portal/app/understanding/understanding.css` (mới) — style scoped.
- `ui/apps/provider-portal/package.json` — thêm dep `mermaid: ^11.16.0`; `ui/package-lock.json` cập nhật.
- Đối chiếu onboarding/incidents của provider-portal: đang là `SectionStub` chờ backend API
  tương ứng — KHÔNG port gì thêm ở bước này (ngoài scope, cần backend contract mới).
- Đối chiếu remote-agents card (iteration 25 cũ): provider-portal đã có tương đương đầy đủ hơn
  ở `app/agents/page.tsx` + `lib/agents.ts` — KHÔNG duplicate.

## Verification đã chạy (bước 1)
- `cd ui/apps/provider-portal && npm install && npm run build` → **xanh**, route `/understanding`
  1.4 kB, không lỗi type/lint. Chạy trực tiếp trên main working tree (không phải worktree).
- Chưa chạy E2E/Playwright cho provider-portal (chưa có thư mục test trong app này).
- Chưa deploy K8s (không rebuild image `aoip-provider-portal`, không rollout) — cố ý, để dồn
  deploy vào cuối mỗi nhóm bước lớn thay vì mỗi route.

## Chưa bắt đầu (bước 5)
Port mới: `ledger` (Error Ledger, khác `audit`/CRAT hash-chain — đừng nhầm), `pipeline` (tracker
11-stage SSE), `trace/[id]` (chi tiết 1 diagnosis + redis-brain, phụ thuộc pipeline), `playbooks`
(CRUD, write-action), `kpi` (dashboard, có thể cần bản tenant-scoped riêng). Nhóm pipeline+trace
nên làm cùng nhau vì chung hạ tầng SSE (nhớ feedback `feedback_traefik_sse_buffering.md`:
body-limit-10m phá SSE, ingress SSE chuyên dụng KHÔNG được gắn middleware đó).
Chưa có route nào trong nhóm này tồn tại ở `PROVIDER_NAV`/`ui/apps/provider-portal/app/` — đây là
port route hoàn toàn mới, không phải đối chiếu/merge như bước 1-4. Cần thêm entry vào
`PROVIDER_NAV` (`lib/nav.ts`) cho từng route mới — đọc kỹ GOVERNING RULE trước khi thêm.

**Quy trình bắt buộc khi agent xong (áp dụng mọi bước từ giờ)**: copy file thủ công từ
`.claude/worktrees/agent-<id>/ui/apps/provider-portal/...` (và `ui/packages/ui-kit/...` nếu có)
sang main bằng `cp`, kiểm tra `git status --short` trong worktree trước để biết chính xác file
nào đổi. KHÔNG git-merge nhánh worktree thẳng — nhánh lệch base rất xa main, merge sẽ xóa nhầm
file không liên quan (đã xảy ra ở bước 1: diff xóa cả PRODUCT_CONTRACT.md, ADR-*, test suite).
Sau copy, luôn chạy lại `cd ui/apps/provider-portal && npm run build` trên main để xác nhận.

## Gotcha quan trọng cho session sau
- **Worktree agent output**: thay đổi của background agent chạy `isolation: worktree` nằm dạng
  uncommitted trong `.claude/worktrees/agent-<id>/`, KHÔNG nằm trên branch riêng theo nghĩa
  commit — `git diff main <branch>` sẽ trả về rác vì branch base cũ. Luôn kiểm tra
  `git status --short` bên trong thư mục worktree để biết chính xác file nào thay đổi, rồi `cp`
  thủ công từng file sang main, KHÔNG git merge/rebase nhánh đó.
- npm dependency (`mermaid`) phải cài lại bằng `npm pkg set` + `npm install` trên main sau khi
  copy file, vì worktree có `node_modules` riêng không mang theo được.

## Blockers
None. **Cả 3 phần pivot đã DONE, merge main, verified (pytest 5990 passed/1 known-fail; npm build
xanh).** Chưa deploy K8s — code mới chỉ chạy local, chưa lên pod thật.

## Next step chính xác (PIVOT đã xong code — còn thiếu DEPLOY để user thấy trên UI thật)
1. **Deploy `aoip-provider-portal`** (image mới) — tìm đúng Makefile target trong
   `k8s/deployments/aoip-portals.yaml` (chưa xác nhận tên trong phiên này, có thể là
   `make deploy-provider-portal` hoặc tương tự — kiểm tra trước khi chạy). Đây là yêu cầu tường
   minh của user: "tôi phải nhìn thấy được toàn bộ trên UI".
2. **Deploy agent thật lên 3 VM lab** (cust-edge/app/db) để probe `connection_scan` mới chạy và
   sinh dữ liệu `connects_to` thật — kiểm tra cơ chế update agent (`omni-remote-agent.service`,
   có tự pull bản mới định kỳ không hay cần redeploy thủ công) TRƯỚC KHI giả định. Nếu không chạy
   bước này, diagram UI sẽ chỉ hiện empty-state "no relational facts yet" — đúng nhưng chưa chứng
   minh được tính năng.
3. Sau khi deploy xong cả 2, curl/mở UI thật xác nhận:
   - `/understanding` trên provider-portal hiển thị theme Ops Console tối (nền surfaces 3 cấp,
     accent tín hiệu, mono cho số liệu).
   - Card "System Diagram" có legend node-shape + section "system topology" xuất hiện (có thể vẫn
     empty-state nếu bước 2 chưa kịp chạy — không phải bug).
4. Sau đó quay lại migration bước 5 (port ledger/pipeline/trace/playbooks/kpi) — TẠM DỪNG từ đầu
   phiên pivot, giờ mới nên tiếp tục.

## Không được làm lại
- Không sửa `ui/app/*` (omni-ui) — chỉ đọc tham khảo, không edit.
- Không git-merge branch của worktree agent thẳng vào main (xem Gotcha).
- Không port thẳng code shadcn/NextAuth từ omni-ui — luôn đổi sang `@aoip/ui-kit` + OIDC pattern.
- Không tự ý retire/xóa `omni-ui` deployment/ingress cho tới khi tất cả 9 bước migration xong.
- Không mở song song nhiều nhóm bước cùng lúc trong CÙNG một phạm vi (migration steps làm tuần
  tự); nhưng pivot phần 2 (backend) và phần 1 (redesign) chạy song song ĐƯỢC vì không đụng file
  chung — đã áp dụng trong phiên này, không phải lỗi.
- Không đổi lại hướng thiết kế "Technical/Ops Console tối" đã chốt qua AskUserQuestion — không
  hỏi lại, không tự ý đổi sang hướng khác.

## Lệnh cần chạy lại
- Build provider-portal: `cd ui/apps/provider-portal && npm run build`.
- Deploy provider-portal (khi tới lúc): rebuild image `aoip-provider-portal`, xem
  `k8s/deployments/aoip-portals.yaml` cho tên Makefile target tương ứng (kiểm tra trước, chưa
  xác nhận trong phiên này).
- Pytest full (nếu đụng backend): `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration`.

## Tài liệu liên quan
- `docs/product/PRODUCT_PROOF.md`, `docs/architecture/ADR-003-backend-frontend-parity.md` (context
  cũ trước quyết định migration mới — có thể cần cập nhật ADR-003 để phản ánh hướng provider/tenant).
- `src/aoip/capabilities/map_system_graph.py` (backend nguồn dữ liệu graph/twin edges).
- `src/gateway/routes/onboarding.py` (`GET /onboarding/readiness`, `GET /onboarding/diagram`).
