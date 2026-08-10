# Blueprint: Hoàn thiện Omni tự vận hành + dọn repo (2026-08-10)

**Mục tiêu:** Không thêm tính năng mới. Sửa sai / xóa dư / đồng bộ lệch / bổ
sung phần thiếu của những gì đã khai báo trong `CLAUDE.md` + `docs/CODEBASE.md`,
và dọn repo cho gọn. Hai track độc lập, Track A trước (rủi ro thấp), Track B
sau (chạm logic vận hành, mỗi domain 1 phase).

**Môi trường (bắt buộc mọi phase):** MacBook = dev, GCP k3s = UAT (không phải
production). Luồng: code+test local (pytest xanh) → commit → push `gitea`
(GCP) → Jenkins build/push Harbor/bump tag → ArgoCD deploy UAT → verify qua
`kubectl` trên UAT thật → `/code-review` (+`security-reviewer` nếu chạm
RBAC/mutation/credential) → cập nhật `docs/handoffs/CURRENT_SESSION.md` →
push cả 2 remote (`gitea` + `origin`).

**Không được làm:** thêm domain/endpoint/collector mới; sửa file lab (không
có hậu tố `.gcp.yaml`) để khớp GCP hoặc ngược lại trừ khi xác nhận là lệch
tài liệu chứ không phải kiến trúc cố ý tách môi trường (ADR 0002); xóa file
thuộc nhóm "retired có annotate" nếu annotation nêu lý do giữ.

**Chế độ thực thi:** Direct mode (không tạo branch/PR riêng — repo dùng
push thẳng `main` + CI/CD tự động theo standing authorization đã có trong
`CLAUDE.md`).

---

## Dependency graph

```
A1 (archive handoff) ──► A2 (dọn manifest/docs retired)

A3 (quyết định ui/ root cũ) ── độc lập, KHÔNG chặn Track B, có thể chạy song song

B0 (đối chiếu INVARIANT vs code thật — nền cho mọi B khác)
   ├─► B1 hitl_decision dead path (đã có bằng chứng #27-31)
   ├─► B2 learning loop chỉ nhận nhãn khen
   ├─► B3 domain security (chưa từng verify lab)
   ├─► B4 domain hardware (chưa từng verify lab)
   ├─► B5 domain application (chỉ đạt urgency medium)
   └─► B6 loop-until-dry: quét 9 domain còn lại tìm lệch mới
                                                                    │
                                                                    ▼
                                                        C1 (tổng hợp + cập nhật CLAUDE.md)
```

B1–B5 độc lập với nhau (không chung file), chạy tuần tự an toàn hoặc song
song nếu có nhiều agent. B6 chạy sau khi B1–B5 đóng để tránh trùng phát hiện.
A3 chạy song song với Track B bất cứ lúc nào (chỉ có 1 mình A2 phụ thuộc A1).

**Đã review đối kháng bằng agent Opus riêng (2026-08-10) — 6 CRITICAL đã sửa
trực tiếp vào các step bên dưới**: (1) A1 dùng sai giả định về heading —
sửa sang cắt theo line offset chính xác; (2) A2 grep pattern ban đầu sẽ
xóa nhầm file LIVE (`omni-fullstack-rbac.yaml`, `src/prober/`,
`.claude/worktrees/`) — sửa sang whitelist theo tên chính xác trong bảng
RETIRED, cấm đụng `src/` và `k8s/deployments/`; (3) A2 thiếu verify
`argocd app diff` trước khi xóa manifest (rủi ro ArgoCD prune object đang
sống trên UAT) — đã thêm; (4) A3 chặn nhầm toàn bộ Track B trong graph cũ —
đã tách; (5) B2 bước 1 dùng pattern cho 0 kết quả thật — đã sửa hướng tìm
đúng cơ chế; (6) B3 có nguy cơ đuổi theo tích hợp FinGuard đã retired (xem
`plans/finguard-to-smart-siem-merge-2026-08-04.md`, Đ25) — đã sửa lại phạm
vi B3 để kiểm tra qua Smart SIEM trước.

---

## Step A1 — Tách archive cho `docs/handoffs/CURRENT_SESSION.md`

**Context brief:** File hiện 3682 dòng / 285KB, đã bị cảnh báo "truncated"
khi session bootstrap load. Đánh số "Đ<n>" trong file KHÔNG đơn điệu theo
thời gian (ví dụ Đ33 xuất hiện 2 lần ở dòng 1062 và 1123, Đ29/Đ30 đảo thứ
tự quanh dòng 1319-1366) — TUYỆT ĐỐI không cắt file theo số hiệu "Đ<n>",
phải cắt theo **line offset cụ thể đã xác nhận bằng grep thật** (không suy
đoán lại).

Ranh giới đã xác nhận (grep `^##* Đ\|^## ` ngày 2026-08-10):
- Dòng 14: `## Đ48` (mới nhất)
- Dòng 99: `## Đ47`
- Dòng 259: `## Đ46`
- Dòng 313: `## Đ45` ← điểm cắt
- Dòng 3600 trở đi: các mục không đánh số Đ (Deliverable/Task list/Tài liệu
  liên quan — thuộc phần cuối file, nội dung tổng kết cũ, đi cùng archive)

**Việc cần làm:**
1. Trước khi làm bất cứ gì, chạy lại `grep -n "^##* Đ\|^## " docs/handoffs/CURRENT_SESSION.md`
   để xác nhận dòng 313 vẫn là ranh giới đúng (file có thể đã đổi từ lúc
   viết blueprint này — KHÔNG tin số dòng cũ nếu file đã bị sửa).
2. Giữ lại trong `docs/handoffs/CURRENT_SESSION.md`: dòng 1–312 (header +
   Đ48/Đ47/Đ46).
3. Chuyển dòng 313 đến hết file sang
   `docs/handoffs/archive/SESSION_ARCHIVE_2026-08.md` (tạo thư mục
   `archive/` nếu chưa có), giữ nguyên nội dung, thêm 1 dòng header ở đầu
   file archive ghi rõ đây là archive, xem file chính để biết trạng thái
   hiện tại.
4. Ở đầu file chính, thêm 1 dòng trỏ tới archive.
5. KHÔNG sửa nội dung bất kỳ mục nào — chỉ di chuyển nguyên văn theo đúng
   line range đã xác nhận ở bước 1.

**Verification:**
- `wc -l docs/handoffs/CURRENT_SESSION.md` xấp xỉ 312 dòng + phần mới thêm.
- `wc -c` tổng của 2 file (chính + archive) khớp với `wc -c` file gốc trước
  khi tách (sai số chỉ bằng đúng phần header mới thêm, không hơn) — dùng
  `wc -c`, không dùng "sai số nhỏ" mơ hồ.
- `git diff --stat` chỉ đụng 2 file (chính + archive mới), không đụng code.

**Exit criteria:** File chính đọc được trong 1 lần `Read` không bị cảnh báo
truncated; archive giữ đủ nội dung dòng 313 trở đi, xác nhận bằng `wc -c`
khớp tuyệt đối.

**Rollback:** `git checkout -- docs/handoffs/` nếu phát hiện mất nội dung.

**Model tier:** default (Sonnet) — việc cơ học, không cần suy luận sâu.

---

## Step A2 — Dọn file/manifest dư thừa đã xác nhận retired

**Context brief:** `CLAUDE.md` liệt kê rõ các Deployment/manifest đã
RETIRED (`omni-analyst/core/executor/prober/worker` — xóa từ commit
`915e509`; `omni-brain-go` — retired 2026-07-22; `omni-ui` — retired
2026-07-06). ⚠️ Grep tự do theo tên (`omni-analyst`, `omni-prober`,
`omni-executor`) sẽ HIT rất nhiều file LIVE không liên quan (đã kiểm chứng:
`k8s/deployments/omni-fullstack-rbac.yaml` chứa ClusterRole
`omni-executor-mutate-lab` load-bearing cho tool `k8s_patch_secret`,
`src/prober/` là package sống được import bởi
`src/workers/temporal_evidence_collector.py` + nhiều test, và
`.claude/worktrees/` chứa nhiều bản sao repo đầy đủ sẽ nhân bản mọi kết
quả). Phạm vi step này CHỈ được xóa file/manifest có TÊN TRÙNG CHÍNH XÁC
với các Deployment đã liệt kê là RETIRED trong CLAUDE.md — không suy rộng
theo grep từ khóa.

**Việc cần làm:**
1. Từ bảng RETIRED trong CLAUDE.md, liệt kê chính xác tên file kỳ vọng đã
   xóa: `k8s/deployments/omni-analyst.yaml`, `omni-prober.yaml`,
   `omni-core.yaml`, `omni-executor.yaml`, `omni-worker.yaml`,
   `omni-brain-go.yaml`, `omni-ui.yaml`, `k8s/services/omni-analyst-service.yaml`,
   và bất kỳ file `k8s/ingress/*.yaml` còn rule trỏ `ai-agent.local`/`omni-ui`
   theo CLAUDE.md ghi đã gỡ.
2. Với MỖI tên trên: `find k8s/ -iname "<tên>"` — nếu file KHÔNG tồn tại
   (đã xóa thật, đúng như CLAUDE.md ghi) → không có việc gì làm, bỏ qua.
   Nếu file VẪN CÒN tồn tại → đây là phát hiện thật (tài liệu nói đã xóa
   nhưng thực tế còn) → xác nhận bằng `git log --all -- <path>` xem có bị
   bỏ sót khi xóa không, rồi mới xóa.
3. Phạm vi tìm kiếm CẤM tuyệt đối: `src/` (mọi file), `k8s/deployments/omni-fullstack*.yaml`,
   `.claude/worktrees/`, bất kỳ path nào KHÔNG nằm trong danh sách tên
   chính xác ở bước 1.
4. KHÔNG đụng `omni-siem-bridge`/`omni-hitl-dispatcher`/`omni-evidence-adapter`
   — CLAUDE.md ghi rõ annotate `scaled-down-intentional`, giữ nguyên.
5. TRƯỚC khi xóa bất kỳ file nào đang được ArgoCD Application track
   (`k8s/gitops/argocd-application.yaml` sources): chạy
   `argocd app diff omni-core` (hoặc `kubectl diff`) để xác nhận xóa file
   này khỏi git sẽ khiến ArgoCD **prune** đúng object đã retired trên UAT
   chứ không phải object đang sống — đây là hệ quả thật của `selfHeal:
   true, prune: true` (Đ48), không phải suy đoán.
6. Xóa các file xác nhận đúng là rác thật, commit riêng khỏi step A1.

**Verification:**
- Sau khi xóa: `pytest tests/ -q --ignore=tests/integration` vẫn xanh trên
  MacBook (local) TRƯỚC khi push.
- Sau khi push + ArgoCD sync trên UAT: `kubectl get deploy,svc -n multi-agent`
  xác nhận KHÔNG có object nào đang chạy biến mất ngoài dự kiến — đối chiếu
  đúng danh sách đã xóa ở bước 6, không suy đoán từ "rollout successful"
  (bài học gotcha `deploy-gateway không build image`).

**Exit criteria:** Danh sách tên ở bước 1 đã đối chiếu 100% với `find`
thật; mọi file xóa đều nằm trong danh sách đó; verify UAT xác nhận đúng
object mất đi là object dự kiến, không có object khác bị ảnh hưởng.

**Rollback:** `git revert` commit này — ArgoCD sẽ tự đồng bộ lại object bị
prune nhầm (nếu có) trong lần reconcile kế tiếp nhờ `selfHeal: true`.

**Model tier:** default (Sonnet), nhưng review kỹ trước khi xóa — dùng
`code-reviewer` agent trước khi commit xóa file.

---

## Step A3 — Quyết định số phận `ui/` root (Next app cũ, 19 route)

**Context brief:** CLAUDE.md ghi rõ: `ui/` root là Next app cũ (~25 route
theo tài liệu, thực đo 19 route trong `ui/app/`), không còn deploy route nào
trỏ tới nó (portal thật là `ui/apps/aoip-provider-web` /
`aoip-tenant-web`), nhưng "xoá source tree là quyết định riêng cần xác nhận
thêm" — tức là CHƯA được phép tự xóa. File có `package.json` riêng, có thể
là dependency cho tooling khác (kiểm tra trước khi động).

**Việc cần làm:**
1. Xác nhận lại 100%: `grep -rl "ui/app\b" k8s/ Jenkinsfile Makefile docs/deployment/`
   — không còn route/build target nào trỏ vào `ui/app` (phân biệt với
   `ui/apps/` — tên gần giống, dễ nhầm).
2. Kiểm tra `ui/package.json` có được bất kỳ workspace/monorepo config nào
   ở `ui/apps/*/package.json` phụ thuộc vào không (`workspaces` field,
   `"@omni/..."` internal package references).
3. Nếu xác nhận 100% độc lập và không deploy: viết báo cáo ngắn liệt kê
   bằng chứng, HỎI USER xác nhận trước khi xóa (đây là quyết định
   CLAUDE.md ghi rõ "cần xác nhận thêm" — không tự quyết dù có standing
   authorization commit/push, vì đây là xóa ~25 route source code, không
   phải thay đổi nhỏ).
4. Nếu user xác nhận: xóa `ui/app/`, `ui/components/` (phần riêng của app
   cũ, không phải `ui/packages/` dùng chung), `ui/package.json` gốc nếu
   không còn ai dùng; cập nhật `docs/CODEBASE.md` bỏ phần mô tả app cũ.
   Nếu user từ chối: đóng step này, ghi lại lý do giữ trong CLAUDE.md.

**Verification:** `make e2e-portal` và mọi E2E hiện có vẫn xanh sau xóa
(không phụ thuộc ngầm vào `ui/app`).

**Exit criteria:** Quyết định rõ ràng (xóa hoặc giữ có lý do) được ghi lại
trong CLAUDE.md, không còn là "chưa xác nhận".

**Rollback:** `git revert`.

**Model tier:** default (Sonnet) cho khảo sát; cần dừng lại hỏi user trước
khi xóa — đây là bước duy nhất trong Track A yêu cầu xác nhận người dùng.

---

## Step B0 — Đối chiếu INVARIANT đã khai báo vs hành vi thật (nền cho B1-B6)

**Context brief:** `CLAUDE.md` liệt kê ~15 INVARIANT (`INV_KNOWLEDGE_NOT_ALERT`,
`INV_DATA_RESIDENCY`, `INV_PUBLIC_PLANE_ISOLATED`,
`INV_NO_RESTART_ON_BROKEN_SPEC`, `INV_READ_BEFORE_MUTATE`,
`INV_NAMESPACE_ISOLATION`, `ERR_REA_NO_PHYSICAL_PROOF`,
`ERR_GOV_UNAUTHORIZED_MUTATION`, v.v.). Trước khi sửa bất kỳ domain nào,
cần 1 bảng đối chiếu: invariant nói gì → code hiện tại có đúng không → có
test nào bảo vệ nó không.

**Việc cần làm:**
1. Với mỗi invariant, `grep` code thật (không đọc lại docs) để xác nhận cơ
   chế enforce còn tồn tại đúng vị trí đã khai báo.
2. Với mỗi invariant, tìm test tương ứng (`grep -rl "<invariant behavior>"
   tests/`). Nếu invariant không có test bảo vệ → đây là "cái thiếu" cần bổ
   sung (chỉ thêm TEST, không thêm logic mới).
3. Xuất bảng: invariant | enforce ở đâu | có test không | trạng thái
   (đúng/lệch/thiếu test) — lưu vào `docs/audit/invariant_audit_2026-08.md`.

**Verification:** Mỗi dòng trong bảng có file:line cụ thể, không có dòng
"chưa kiểm tra" bỏ trống.

**Exit criteria:** Bảng đầy đủ, là input trực tiếp cho B1-B6 (không phase
nào bắt đầu sửa domain mà chưa có dòng tương ứng trong bảng này).

**Model tier:** strongest (Opus) — đây là bước suy luận đối chiếu quan
trọng nhất, sai ở đây lan ra toàn bộ Track B.

---

## Step B1 — `hitl_decision` dead path (task #27-31, bằng chứng đã có)

**Context brief:** Memory `project_ground_truth_audit_2026_08_04` +
`project_hitl_implementation_2026_08_04` đã định lượng: `hitl_decision`
chết 100% (silent), `case_ledger` chỉ 2/1000+ quyết định thật, root cause
là nhánh chẩn đoán chính không gắn ack-keyboard. Task #27/#28/#29/#30 được
ghi là "đã code+deploy sống, 4 commit local CHƯA push" — cần xác nhận trạng
thái thật hiện tại (có thể đã push ở phiên sau, verify lại đừng tin memory
cũ).

**Việc cần làm:**
1. `git log --oneline -20 -- src/workers/analyst_agentic_loop.py` (hoặc file
   liên quan HITL) — xác nhận 4 commit đó đã push hay còn local-only.
2. Nếu còn local-only: test lại trên MacBook, verify UAT (theo luồng môi
   trường bắt buộc), push.
3. Nếu đã push từ trước: verify sống qua Postgres/Redis thật trên UAT xem
   `hitl_decision`/`case_ledger` có ghi nhận đúng chưa (dùng cùng phương
   pháp định lượng đã dùng trong audit 2026-08-04 — không suy đoán từ log).
4. Nếu vẫn chết: đọc lại root cause đã xác định (ack-keyboard không gắn ở
   nhánh chẩn đoán chính), sửa ĐÚNG root cause đó — không thêm cơ chế mới.

**Verification:** Query Postgres UAT thật: tỉ lệ `hitl_decision` non-null
tăng so với baseline 100% chết đã ghi trong audit.

**Exit criteria:** `hitl_decision` được ghi nhận cho luồng chẩn đoán chính,
xác nhận bằng số liệu Postgres thật, không phải "code đã sửa" suông.

**Model tier:** strongest (Opus) cho chẩn đoán, default cho code sửa.

---

## Step B2 — Vòng học chỉ nhận nhãn khen (`accepted=False` không có call site)

**Context brief:** Memory `project_learning_loop_broken_labels` ghi:
nhánh FROZEN là code chết, nút "đã đọc" bị đọc thành "đồng ý", HITL verdict
bị vứt. Đây là lệch giữa thiết kế (vòng học 2 chiều: khen+chê) và code thật
(chỉ nhận khen).

**Việc cần làm:**
1. ⚠️ Đã xác nhận trước (2026-08-10): `grep -rn "accepted=False\|accepted = False" src/`
   trả về **0 kết quả** — literal đó không còn tồn tại (có thể đã đổi tên biến/enum
   từ lúc memory được ghi, hoặc pattern đã sai ngay từ đầu). KHÔNG kết luận
   "đã fix" chỉ vì 0 hit — phải tìm đúng cơ chế hiện tại.
2. Đọc các file có khả năng liên quan cao nhất (xác nhận bằng grep
   `"FROZEN\|accepted"` không kèm test): `src/workers/advisory_ack.py`,
   `src/services/learning_promoter/promoter.py`,
   `src/services/learning_promoter/advisory_promoter.py`,
   `src/workers/playbook_governor.py`, `src/services/case_ledger/advocacy.py`.
   Xác định tên field/enum THẬT hiện tại dùng để biểu diễn "reject"/"không
   đồng ý" (có thể không còn là boolean `accepted`, có thể là enum verdict).
3. Tìm nơi UI/Telegram gửi tín hiệu reject — trace xem nó có đi tới đúng
   hàm ghi nhãn "chê" tìm được ở bước 2 không, hay bị nuốt ở tầng nào
   (ack-đã-đọc bị hiểu nhầm thành đồng ý, như memory mô tả).
4. Sửa ĐÚNG điểm nuốt tín hiệu đó (route lại cho đúng, không thêm cơ chế
   phản hồi mới, không đổi tên field nếu không bắt buộc).

**Verification:** Test tái tạo: gửi 1 reject giả lập qua đường thật (không
mock tầng cần sửa) → xác nhận `accepted=False` được ghi vào DB/RAG.

**Exit criteria:** Nhãn "chê" đi tới đích, có test integration bảo vệ.

**Model tier:** default (Sonnet).

---

## Step B3 — Domain `security` (chưa từng verify được trong lab)

**Context brief:** Bảng 9-domain trong CLAUDE.md ghi domain `security` (do
`siem_reasoning.py`, FinGuard) trạng thái ❌ "chưa kiểm được trong lab".
⚠️ Đã xác nhận: FinGuard đã được **gộp vào Smart SIEM nội bộ** theo
`plans/finguard-to-smart-siem-merge-2026-08-04.md` (Đ25) — TRƯỚC khi làm
bất cứ gì, đọc file plan đó để biết trạng thái merge đã đóng tới đâu.
KHÔNG được khôi phục/thêm lại tích hợp FinGuard độc lập (namespace/secret
FinGuard riêng) nếu nó đã bị thay thế — làm vậy là đi ngược lại quyết định
kiến trúc đã chốt, không phải "bổ sung phần thiếu".

**Việc cần làm:**
1. Đọc `plans/finguard-to-smart-siem-merge-2026-08-04.md` toàn bộ, xác
   định: merge đã hoàn tất tới bước nào (S0-S4 trong plan đó), phần nào
   còn dang dở.
2. Đọc `siem_reasoning.py` + module Smart SIEM hiện tại (không phải
   FinGuard cũ) để xác định vì sao domain `security` chưa kiểm được —
   khả năng cao là do phần merge còn dang dở (ví dụ ghi trong plan cũ:
   "PlaybookMatcher sau khi bỏ gate finguard có match được playbook
   security thật không, hay bảng omni_admin.playbook (hiện 0 dòng) cần
   seed trước" — kiểm tra đúng bảng này trên UAT thật).
3. Nếu nguyên nhân là phần merge chưa đóng (ví dụ playbook chưa seed): đó
   là việc CÒN THIẾU của công việc merge đã thiết kế trước đó — hoàn tất
   nốt theo đúng plan merge, không tự thiết kế lại.
4. Chỉ sau khi xác nhận merge đã đóng đủ: chạy 1 sự kiện test thật qua
   domain security (qua đường Smart SIEM), xác nhận pipeline tới cuối
   (advisory/HITL) hoạt động.

**Verification:** 1 sự kiện security thật đi hết pipeline, có trace ID xác
nhận qua `kubectl logs`/Redis trace store trên UAT.

**Exit criteria:** Domain `security` chuyển từ ❌ sang ✅ trong bảng
9-domain của CLAUDE.md, có bằng chứng cụ thể kèm theo (không phải tự đánh
giá).

**Model tier:** strongest (Opus) — domain phức tạp, rủi ro cao (chạm
FinGuard/credential).

---

## Step B4 — Domain `hardware` (không kiểm được trên OrbStack/lab)

**Context brief:** CLAUDE.md ghi domain `hardware` ❌ "không kiểm được trên
OrbStack (không có cảm biến)". Đây là giới hạn môi trường lab, KHÔNG phải
bug. GCP VM (UAT) có thể cũng không có cảm biến phần cứng thật (là VM ảo
hóa) — cần xác định xem domain này có khả năng verify được trên UAT hay
không trước khi cố sửa.

**Việc cần làm:**
1. Kiểm tra collector hardware (nếu tồn tại) có đọc được `/sys`, `lm-sensors`,
   hoặc tương đương trên VM GCP không — VM cloud thường KHÔNG expose cảm
   biến phần cứng thật, nên domain này có thể vĩnh viễn không kiểm được
   trong mọi môi trường hiện có.
2. Nếu đúng vậy: đây KHÔNG phải việc cần sửa — ghi rõ vào CLAUDE.md lý do
   (giới hạn môi trường ảo hóa, không phải nợ kỹ thuật) thay vì để nguyên
   dấu ❌ mơ hồ. Đây là "đồng bộ tài liệu cho đúng thực tế", không phải
   thêm code.
3. Nếu collector chưa tồn tại nhưng đã được khai báo thiết kế ở đâu đó
   (kiểm tra kỹ, không suy đoán) → đó là "thiếu" hợp lệ, bổ sung.

**Verification:** CLAUDE.md phản ánh đúng giới hạn thật, không còn nhập
nhằng "chưa kiểm" khi thực chất là "không thể kiểm trong môi trường hiện
có".

**Exit criteria:** Trạng thái domain hardware rõ ràng, đúng sự thật kỹ
thuật.

**Model tier:** default (Sonnet).

---

## Step B5 — Domain `application` (chỉ đạt urgency `medium`)

**Context brief:** CLAUDE.md ghi domain `application` (do `collectors/logs.py`,
`log_surge_probe.py`) ⚠️ "chỉ đạt urgency medium" — tức pipeline chạy được
nhưng đánh giá mức độ nghiêm trọng thấp hơn kỳ vọng thiết kế.

**Việc cần làm:**
1. Đọc `assess_domain_severity` cho domain application, so với os_host/
   database (đạt critical) — tìm điểm khác biệt trong ngưỡng/logic tính
   severity.
2. Xác định đây là ngưỡng tĩnh đặt sai (dễ sửa, đúng phạm vi "sửa cái sai")
   hay do log_surge_probe chưa sinh đủ tín hiệu mạnh (cần bằng chứng thật
   từ 1 lần chạy test, không đoán).
3. Sửa đúng điểm gốc tìm được — KHÔNG hạ thấp ngưỡng tùy tiện để "cho qua",
   phải có lý do kỹ thuật cụ thể khớp với cách os_host/database đã làm
   đúng.

**Verification:** Lặp lại kịch bản test đã dùng khi audit 2026-07-30 (log
surge giả lập), xác nhận urgency đạt đúng mức kỳ vọng theo thiết kế.

**Exit criteria:** Domain application đạt urgency tương xứng mức độ sự cố
giả lập, có bằng chứng lặp lại được.

**Model tier:** default (Sonnet).

---

## Step B6 — Loop-until-dry: quét toàn bộ 9 domain tìm lệch còn sót

**Context brief:** Sau B1-B5 (5 vấn đề đã biết trước), quét lại toàn bộ 9
domain (bao gồm cả `os_host`/`database`/`service`/`kubernetes`/`storage`
đã từng ✅ — trạng thái có thể đã trôi kể từ lần verify cuối) để tìm lệch
mới phát sinh, dùng cùng phương pháp B0.

**Việc cần làm:**
1. Với mỗi domain, chạy lại đúng kịch bản verify đã dùng lần trước (ghi
   trong bảng 9-domain của CLAUDE.md, cột "Trạng thái đã kiểm bằng lỗi
   thật") — không phát minh kịch bản mới.
2. Nếu domain vẫn ✅ như cũ → đóng, không sửa gì (tránh sửa cái không hỏng).
3. Nếu phát hiện lệch mới → tạo 1 phase con B6.x riêng theo đúng khuôn các
   step B1-B5 ở trên, xử lý xong mới quét domain tiếp theo.
4. Lặp lại cho đến khi 1 vòng quét đầy đủ 9 domain không phát hiện gì mới
   → đóng Track B.

**Verification:** Mỗi domain có 1 dòng bằng chứng verify mới (ngày +
kết quả), cập nhật bảng 9-domain trong CLAUDE.md.

**Exit criteria:** 1 vòng quét trọn vẹn không phát hiện lệch mới nào.

**Model tier:** strongest (Opus) cho vòng quét đầu, default cho các vòng
lặp lại sau khi đã quen mẫu.

---

## Step C1 — Tổng hợp, cập nhật CLAUDE.md, đóng blueprint

**Context brief:** Sau khi Track A + Track B đóng, `CLAUDE.md` cần phản
ánh đúng trạng thái mới (bảng 9-domain cập nhật, quy ước môi trường
MacBook=dev/GCP=UAT được thêm chính thức, mục RETIRED cập nhật nếu Step A3
xóa `ui/` root).

**Việc cần làm:**
1. Cập nhật `CLAUDE.md`: thêm mục quy ước môi trường (nội dung đã lưu ở
   memory `project_env_convention_macbook_dev_gcp_uat`), cập nhật bảng
   9-domain theo kết quả B1-B6, cập nhật mục RETIRED nếu có.
2. Viết báo cáo tổng kết ngắn (≤ 1 trang) liệt kê: đã sửa gì, đã xóa gì, đã
   đồng bộ gì, đã bổ sung gì — theo đúng 4 nhóm user yêu cầu ban đầu.
3. Cập nhật `docs/handoffs/CURRENT_SESSION.md` (mục hiện hành, không phải
   archive) với checkpoint cuối cùng.
4. Push cả 2 remote.

**Verification:** `pytest tests/ -q --ignore=tests/integration` xanh toàn
bộ; `git status` sạch; CLAUDE.md không còn mục nào tự mâu thuẫn với trạng
thái cluster UAT thật.

**Exit criteria:** Blueprint đóng, báo cáo đã gửi user.

**Model tier:** default (Sonnet).
