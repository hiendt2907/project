# Current Session Handoff

## Deliverable hiện tại
1. Xác nhận bằng browser thật (không chỉ text Mermaid) rằng System Topology diagram trên
   provider-portal render đúng — carry-over từ phiên trước.
2. **Domain cutover omni/portal → provider/tenant** (yêu cầu trực tiếp mới của user: "xoá luôn cái
   domain omni và portal đi, đã chuyển qua provider và tenant rồi mà") — DONE, xem "Đã hoàn thành".

## Definition of Done
1. Provider-portal hiển thị theme Technical/Ops Console tối (đã chốt, không đổi).
2. `/understanding` có sơ đồ kiến trúc thật (subgraph host + service edges) — **đã xác nhận bằng
   E2E thật trên browser** (không còn chỉ verify qua text Mermaid).
3. `omni-ui` (Deployment/Service/Ingress domain omni/portal) không còn tồn tại trong cluster —
   portal thật duy nhất là provider/tenant.
4. `make e2e-portal` là gate thật cho provider/tenant portal, không còn trỏ vào omni-ui.

## Đã hoàn thành (phiên này)
1. **Phát hiện root-cause của toàn bộ nghi ngờ "chưa verify được UI thật" ở phiên trước**: gate
   `make e2e-portal` (và `ui/e2e/understanding.spec.ts`) trước đây chạy lên pod **omni-ui** (Next
   app cũ ở `ui/app/`), KHÔNG phải `ui/apps/provider-portal` — nơi thực sự chứa mọi thay đổi
   diagram/understanding của phiên trước. Hai implementation hoàn toàn khác nhau (client component
   cũ vs server component mới). Verify trước đó vô tình test nhầm app.
2. **Domain cutover — xoá omni-ui khỏi cluster và git**:
   - `kubectl delete ingress omni-ui omni-ui-sse; kubectl delete deployment/service omni-ui` (namespace
     `multi-agent`) — đã chạy live.
   - `git rm k8s/deployments/omni-ui.yaml`; gỡ 2 khối Ingress rule (`omni-ui`, `omni-ui-sse`) khỏi
     `k8s/ingress/ai-agent-local.yaml`, còn lại domain map trỏ sang `k8s/ingress/aoip-portals.yaml`.
   - `Makefile::hosts-update` — bỏ `portal.ai-agent.local omni.ai-agent.local`, thêm
     `dex.ai-agent.local provider.ai-agent.local tenant.ai-agent.local` (đồng bộ với `/etc/hosts`
     thật đang dùng trong lab).
   - CLAUDE.md "DEPLOYMENT STATE" cập nhật: `omni-ui` đánh dấu RETIRED 2026-07-06, ghi rõ portal
     thật là provider/tenant. Root `ui/` (Next app cũ, ~25 route) **CHƯA xoá source** — không còn
     route nào tới nó nhưng chưa xác nhận 100% feature parity đã port hết sang provider/tenant;
     xoá toàn bộ source tree là quyết định riêng, cần user xác nhận thêm trước khi làm (rủi ro mất
     code chưa port: SIEM/KPI dashboard/ledger/trace/admin/workers pages).
3. **Rebuild + redeploy `aoip-provider-web` THẬT** (`docker build -f apps/provider-portal/Dockerfile`
   → `kubectl rollout restart deployment/aoip-provider-web`) — lần đầu tiên trong toàn bộ pivot này
   mã nguồn `ui/apps/provider-portal/` (diagram fix, mermaid-diagram.tsx, diagram-utils.ts) thực sự
   chạy trên pod, không chỉ tồn tại trong working tree.
4. **Migrate `scripts/e2e_portal_release_gate.sh`** từ omni-ui (port-forward + NextAuth login) sang
   `tests/e2e_portals` (provider/tenant thật qua Traefik LB + Dex OIDC, không cần port-forward vì
   `/etc/hosts` đã map thẳng LB IP). `make e2e-portal` nay chạy 13 test thật.
5. **Chạy `tests/e2e_portals` trên provider-portal thật sau rebuild — 13/13 PASS**, bao gồm:
   Overview control-tower số thật, CSP nonce, OIDC login/logout qua Dex, cookie isolation
   provider/tenant, session expiry 401, Human Inbox answer-question, VÀ `/understanding` với
   `facts-table`/`competency-table` — đây là proof thật đầu tiên (browser thật, không phải script)
   rằng code diagram của phiên trước hoạt động đúng trên pod thật.
6. **Fix 1 test lỗi thật phát hiện trong lúc chạy** (không phải regression từ việc trên) —
   `provider_overview.spec.ts` dùng `page.getByTestId("facts-table")` không scope theo tenant, giờ
   có 2 tenant (`staging-sim`, `tenant-replay-01`) nên strict-mode violation (2 phần tử khớp). Fix:
   scope locator vào `understanding-staging-sim` trước khi tìm `facts-table`/`competency-table`.
7. **Dedupe bug thật phát hiện + fix xong**: `render_api_sequence_diagram`
   (`src/pkg/onboarding/discovery_doc.py:156`) emit edge trùng lặp khi 2 port_scan entries cùng
   `port` (thấy `svc_6379`/`redis-server (6379)` lặp 2 lần trong diagram tenant `staging-sim` thật —
   đây chính là "cái box lửng lơ" user chụp ảnh phàn nàn ở phiên trước, KHÔNG phải bug CSS/theme
   như "Blockers" phiên trước từng nghi ngờ). Fix: dedupe theo `port` trước khi cắt `[:20]`. Test
   mới `tests/test_diagram_topology.py::TestRenderApiSequenceDiagram` (2 test). Rebuild
   `multi-agent-system:latest`, rollout `omni-onboarding`/`omni-fullstack`/`aoip-provider-portal`/
   `aoip-tenant-portal`. Xác nhận qua Redis thật (`omni:onboarding:diagram:staging-sim:latest`):
   diagram tenant thật không còn edge trùng. Full suite `5992 passed` sau rebuild.

## ⚠️ PHIÊN NÀY ĐANG TẠM DỪNG — theo yêu cầu trực tiếp của user ("tạm dừng đi")
Sau khi hoàn thành domain cutover + dedupe fix (mục 1-7 dưới), user đưa thêm 2 chỉ thị mới rồi yêu
cầu tạm dừng trước khi kịp verify lại:
8. **Yêu cầu mới #1**: "không được hardcode, không giả chế dữ liệu — phải discovery thực sự, giả
   lập dữ liệu chỉ được bằng cách cài đặt lên VM OrbStack thật" — đã là quyết định chốt (xem
   "Quyết định đã chốt"), không có thay đổi code mới cho yêu cầu này riêng lẻ.
9. **Yêu cầu mới #2 (kèm 2 ảnh tham khảo diagram kiến trúc chuyên nghiệp)**: "topology system
   architect và topology api architect vẽ như thế này" (layered Internet/Firewall/Router/Servers;
   Client→API Gateway→Services→DB) — **ĐÃ IMPLEMENT** trong `src/pkg/onboarding/discovery_doc.py`:
   - `_EDGE_TIER_NAMES`/`_DATA_TIER_NAMES`/`_service_tier()`: phân loại service Edge/App/Data theo
     tên process THẬT (nginx/haproxy/traefik/envoy/... = edge; mariadbd/postgres/redis-server/... =
     data; còn lại = app). Không đoán, chỉ match tên process discovery thật trả về.
   - `render_system_topology_diagram`: đổi từ `graph LR` per-host subgraph sang `graph TB` với
     3 subgraph tier (`tier_edge`/`tier_app`/`tier_data`) xếp theo thứ tự lớp kiến trúc thật.
   - `render_api_sequence_diagram`: thêm gateway-detection — nếu có service edge-tier trong danh
     sách port đã dedupe, vẽ `Client → Gateway (hexagon) → other services`; nếu không có gateway,
     fallback flat client-fanout như cũ.
   - Test mới: `tests/test_diagram_topology.py` — `test_services_grouped_into_edge_app_data_tiers`,
     `test_no_gateway_service_falls_back_to_flat_client_fanout`,
     `test_edge_tier_service_renders_as_gateway_between_client_and_backends`. Full file
     `.venv/bin/python -m pytest tests/test_diagram_topology.py -q` → **9 passed**.
   - Rebuild `multi-agent-system:latest` + rollout `omni-onboarding`, `omni-fullstack`,
     `aoip-provider-portal`, `aoip-tenant-portal` — **đã chạy xong, rollout status confirmed OK**
     trước khi user gõ "tạm dừng đi".
10. **User gõ "tạm dừng đi" ngay sau rollout #9** — dừng lại theo đúng yêu cầu, KHÔNG tiếp tục thêm
    thay đổi code/rebuild/deploy nào nữa cho tới khi có chỉ thị mới.

## Phiên tiếp theo (sau resume) — re-run verify + phát hiện + fix thêm 1 bug thật
11. **`NS=multi-agent make e2e-portal` re-run sau rebuild #9 → 13/13 PASS.** (Môi trường thiếu
    browser binary Chromium giữa chừng — `Executable doesn't exist ... chrome-headless-shell` — do
    cache Playwright bị mất; fix bằng `npx playwright install chromium`, không liên quan code.)
12. **Browser thật xác nhận layout tier — PHÁT HIỆN BUG THẬT #2 (đảo ngược tier)**: chụp
    screenshot `/understanding` thật (login `owner@aoip.dev`), thấy `System Topology` render
    **Data (mariadbd/redis-server) Ở TRÊN, Edge/Gateway (nginx) Ở DƯỚI** — ngược hẳn thứ tự kiến
    trúc yêu cầu, dù code khai subgraph đúng thứ tự `tier_edge → tier_app → tier_data`.
    - **Root cause thật** (đọc Mermaid source thật từ Redis
      `omni:onboarding:diagram:staging-sim:v13782`): mỗi cặp host có **2 fact `connects_to` hai
      chiều** (`cust-edge→cust-app` VÀ `cust-app→cust-edge`, tương tự cho app/db) — do
      `connection_scan` quan sát cùng 1 kết nối TCP từ CẢ HAI đầu. 2 cạnh ngược chiều tạo thành
      chu trình (cycle) 2 node, khiến thuật toán dagre của Mermaid không xác định được thứ tự
      rank top-to-bottom nữa và tự ý đảo ngược — bug thật về **dữ liệu trùng lặp gây vỡ layout**,
      không phải lỗi CSS/thứ tự code.
    - **Fix** (`src/pkg/onboarding/discovery_doc.py::render_system_topology_diagram`): thêm
      `host_tier: dict[str, str]` ghi tier của từng host lúc build subgraph; trong vòng lặp
      `cross_edges`, dedupe theo cặp KHÔNG hướng `(frozenset({subject,obj}), predicate)` — nếu
      cặp đã render, bỏ qua fact còn lại; hướng cạnh còn lại được chọn theo tier-rank thấp→cao
      (`_TIER_ORDER.index`) để luôn vẽ edge→app→data, không bao giờ ngược. Không suy đoán dữ liệu
      mới — chỉ gộp 2 fact vốn dĩ đã khẳng định cùng một liên kết vật lý.
    - Test mới: `tests/test_diagram_topology.py::test_reciprocal_connects_to_facts_collapse_to_one_edge_ordered_edge_to_data`.
      File test → **10 passed**. Full suite `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration`
      → **5995 passed, 5 deselected, 1 failed** (`test_register_then_real_system_metrics_emitted_through_real_pipeline`
      — flake KHÔNG liên quan, do đo `psutil` tải máy thật lúc test chạy, tự route sang
      `omni-knowledge-evidence` thay vì `omni-diagnostic-evidence` tuỳ tải; đã re-run riêng xác
      nhận không phải regression từ thay đổi này).
    - Rebuild `multi-agent-system:latest`, rollout `omni-onboarding`/`omni-fullstack` — xong.
    - Force-regenerate diagram tenant `staging-sim` bằng cách exec trực tiếp `regenerate_diagrams()`
      trong pod `omni-onboarding` đã rollout (không đợi probe cycle 1h) → version mới `v13819` rồi
      `v13855` (tự động lần 2 từ probe loop) — cả 2 xác nhận qua Redis: chỉ còn **1 cạnh
      `connects_to`** mỗi cặp, không còn cặp ngược.
    - **Browser screenshot thật xác nhận lần cuối** (`/understanding`, tenant `staging-sim`):
      thứ tự đúng top-to-bottom **Edge/Gateway (nginx cust-edge) → connects_to → Application
      (python3 cust-app) → connects_to → Data (cust-db: redis-server, mariadbd)**, mỗi liên kết
      chỉ 1 mũi tên. Giống đúng 2 ảnh tham khảo kiến trúc user gửi.
13. **API Sequence diagram (gateway pattern) cũng xác nhận đúng bằng browser thật**: `Client →
    nginx (80) — Gateway (hexagon) → port-38973, port-37903` — pattern Client→Gateway→Services
    đúng yêu cầu, không phải chỉ đọc code/test.

## Phiên tiếp theo (cùng lượt) — user yêu cầu vẽ "như draw.io" + PHÁT HIỆN root-cause thật #3 (CSP chặn toàn bộ style Mermaid)
14. **User gửi ảnh chụp riêng System Topology + yêu cầu**: "vẽ nó như topology tương tự như tôi vẽ
    trên draw.io, với những ô, đường line, ghi chú rõ ràng" — ảnh cho thấy mỗi box gần như đen
    tuyền, không phân biệt được tier nào với tier nào (dù đã fix layout ở #12), không giống
    draw.io (không màu, viền mờ).
15. **Điều tra sâu — phát hiện root-cause thật #3, nghiêm trọng hơn nhiều so với tưởng ban đầu**:
    ban đầu tưởng chỉ là "Mermaid theme xấu" nên thêm `classDef`/`style`/`linkStyle` màu theo tier
    vào `render_system_topology_diagram` + `render_api_sequence_diagram` (`_TIER_NODE_STYLE`,
    `_TIER_CLUSTER_STYLE`, `_LINK_STYLE` — màu cyan/tím/cam phân biệt Edge/App/Data). Rebuild +
    regenerate xong nhưng browser vẫn render đen tuyền y hệt. Dùng `page.evaluate(getComputedStyle)`
    thật trên node polygon: `style` attribute đúng `fill:#123c4d !important` nhưng **computed style
    vẫn ra `rgb(0,0,0)`** — khớp với giá trị mặc định của SVG khi HOÀN TOÀN không có CSS nào áp
    dụng. Kiểm tra response header thật (`curl -D-`) → **`Content-Security-Policy: style-src 'self'`
    (không có `'unsafe-inline'`, không nonce)** — CSP này chặn ÂM THẦM (không log lỗi rõ ràng ở nơi
    dễ thấy) toàn bộ `<style>` + `style=""` mà mermaid.js tự sinh runtime (thư viện không hỗ trợ CSP
    nonce). Đây LÀ root-cause thật của "hộp đen không viền" xuyên suốt CẢ session này (mọi
    screenshot từ đầu tới giờ đều bị ảnh hưởng, kể cả trước khi có tier-color) — không phải theme
    Mermaid xấu, không phải bug code Python.
    - **Fix**: `ui/apps/provider-portal/middleware.ts` — `style-src 'self'` → `style-src 'self'
      'unsafe-inline'`. Chỉ nới `style-src`, `script-src` vẫn strict nonce/`strict-dynamic` không
      đổi. Đúng theo khuyến nghị chuẩn `~/.claude/rules/web/security.md` (`style-src 'self'
      'unsafe-inline' ...`). Rebuild `aoip-provider-web:latest` (context `ui/`, không phải root —
      `docker build -f apps/provider-portal/Dockerfile .` chạy TỪ THƯ MỤC `ui/`), rollout restart.
    - Xác nhận header thật sau rollout: `curl -D-` → `style-src 'self' 'unsafe-inline'` áp dụng.
    - **Browser screenshot thật xác nhận cuối cùng**: màu hiển thị đúng — Edge/Gateway (cyan,
      `#123c4d`/`#4fc3f7`), Application (tím, `#2b1f45`/`#b388ff`), Data (cam, `#402712`/`#ffb74d`),
      viền rõ 2px, đường nối `connects_to` màu xanh nhạt `#8ecae6` 2px có mũi tên rõ, subgraph
      cluster `cust-db` lồng đúng cấu trúc — đúng phong cách draw.io user yêu cầu.
    - `NS=multi-agent make e2e-portal` sau rebuild → **13/13 PASS** (bao gồm cả test CSP nonce vẫn
      0 lỗi — nới `style-src` không phá strict CSP cho script).
16. Dọn 2 file Playwright tạm bị sót lại (`tests/e2e_portals/screenshot_diagram{,2}.mjs`) — đã xoá,
    không commit nhầm.

## Việc CÒN LẠI — chưa xong, next step chính xác (session tiếp theo đọc đây trước)
1. Quyết định số phận source tree `ui/` root (Next app cũ, ~25 route) — hiện KHÔNG còn route nào
   tới nó (domain đã xoá) nhưng source vẫn còn trong repo. CẦN xác nhận riêng của user trước khi
   xoá hẳn (rủi ro mất code chưa xác nhận đã port hết sang provider/tenant: SIEM/KPI
   dashboard/ledger/trace/admin/workers pages).
2. Root `ui/e2e/understanding.spec.ts` (test cũ target omni-ui) giờ là dead test — không cần fix
   count 3→4 mismatch trừ khi quyết định #1 là "giữ + tiếp tục maintain root ui/".
3. **Chưa commit/push gì** — toàn bộ domain cutover + 2 dedupe fix + tier redesign + tier-color
   styling + CSP style-src fix vẫn ở working tree. Chờ user xác nhận trước khi tách commit (nhiều
   root-cause độc lập, có thể tách nhiều commit theo mục "Đã hoàn thành" #1-16 — gợi ý nhóm CSP fix
   (#15, `ui/apps/provider-portal/middleware.ts`) thành 1 commit riêng vì đây là fix bảo mật/hạ
   tầng khác hẳn nhóm diagram-logic).
4. **Nên kiểm tra `ui/apps/tenant-portal/middleware.ts`** có cùng `style-src 'self'` không có
   `'unsafe-inline'` hay không — CHƯA kiểm tra trong phiên này vì tenant-portal không render
   Mermaid diagram nên không phải blocker, nhưng cùng pattern CSP có thể lặp lại nếu tenant-portal
   sau này thêm bất kỳ thư viện client-side nào tự sinh inline style.
5. Diagram tier hiện chỉ có 3 host thật trong VM lab (cust-edge/cust-app/cust-db, mỗi host 1-2
   service) — chưa test với topology phức tạp hơn (nhiều host cùng tier, nhiều cạnh chéo). Không
   phải blocker, chỉ là phạm vi dữ liệu thật hiện có trong lab.

**Next action khi resume: hỏi user có muốn tách commit theo root-cause hay giữ nguyên working tree
chờ thêm việc; và hỏi quyết định #1 (số phận `ui/` root) + #4 (audit CSP tenant-portal).**

## Quyết định đã chốt
- Hướng thiết kế "Technical/Ops Console tối" — không đổi lại.
- Diagram "2+ service ambiguous" → trỏ vào subgraph cluster, KHÔNG fan-out n×m.
- `connects_to`/`hosts` chỉ sinh từ dữ liệu thật (remote_ip resolve qua registry, hoặc
  port_scan/service_topology tự báo cáo) — không đoán, không hardcode, không mock. Dữ liệu giả lập
  DUY NHẤT hợp lệ là cài đặt service thật lên VM lab OrbStack (`conn-keepalive*.service` trên
  cust-edge/cust-app/cust-db) để discovery THẬT quét ra — không sinh fake fact trong code.
- `omni-ui`/domain omni-portal: RETIRED, không tạo lại trừ khi có quyết định kiến trúc mới.

## Verification đã chạy (phiên này)
- `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` → **5995 passed, 5 deselected,
  1 failed** (flake `psutil` không liên quan, xem mục "Đã hoàn thành" #12).
- `NS=multi-agent make e2e-portal` → **13 passed** — chạy 4 lần trong toàn phiên (trước rebuild
  dedupe #7, sau rebuild #7, sau rebuild tier+reciprocal-edge fix #9/#12, và sau CSP style-src fix
  #15), cả 4 lần xanh — bao gồm test CSP nonce vẫn 0 lỗi sau khi nới `style-src`.
- `kubectl -n multi-agent get deploy,svc,ingress` → xác nhận `omni-ui` không còn tồn tại; 3 Ingress
  omni/portal đã xoá (`omni-ui`, `omni-ui-sse` deleted, ingress `omni-gateway` giữ nguyên vì phục vụ
  domain riêng `gateway.ai-agent.local`).
- Đọc trực tiếp Redis thật (`omni:onboarding:diagram:staging-sim:*`) nhiều lần trong phiên — diagram
  tenant thật (không phải test giả lập) xác nhận hết trùng edge port VÀ hết cạnh `connects_to`
  ngược chiều.
- **Browser thật (Playwright script tạm, KHÔNG phải chỉ đọc text Mermaid)**: login
  `owner@aoip.dev`, chụp `/understanding` full-page, xác nhận bằng mắt layout tier đúng thứ tự
  Edge→Application→Data và API-sequence Gateway pattern đúng. Đây là bằng chứng browser-thật đầu
  tiên cho riêng phần tier-redesign + reciprocal-edge fix (khác với minh chứng #5 ở trên vốn chỉ
  cover phần dedupe-port + cấu trúc chung, chưa có tier).

## Branch và commit
`main`, HEAD `ec12d0a` — **chưa commit** gì trong phiên này (working tree có thêm thay đổi so với
phiên trước: xoá `k8s/deployments/omni-ui.yaml`, sửa `k8s/ingress/ai-agent-local.yaml`, `Makefile`,
`CLAUDE.md`, `scripts/e2e_portal_release_gate.sh`, `tests/e2e_portals/specs/provider_overview.spec.ts`,
cộng dồn thay đổi cũ từ phiên trước chưa commit). Chờ user xác nhận trước khi tách commit + push
(nhiều root-cause độc lập, có thể tách nhiều commit).

## Next step chính xác
1. Hỏi user F5 xác nhận UI thật lần cuối (khả năng cao đã xong sau dedupe fix).
2. Quyết định số phận source tree `ui/` root (xoá hẳn hay giữ tham khảo).
3. `git add` theo từng root-cause + commit, push (chỉ khi user yêu cầu).
