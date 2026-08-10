# Current Session Handoff

**Cập nhật:** 2026-08-10 (Đ49 — ĐÃ ĐÓNG. Blueprint dọn dẹp + hoàn thiện Omni tự vận hành (Track A+B,
`plans/omni-finish-autonomous-sre-and-repo-cleanup-2026-08-10.md`) VÀ gộp FinGuard→Smart SIEM nội
bộ (S0-S4, `plans/finguard-to-smart-siem-merge-2026-08-04.md`) — theo yêu cầu trực tiếp của user
"merge luôn vào omni đi, nó là tính năng có sẵn và phải có của omni, không phải thêm tính năng
mới". domain `security` chuyển từ ❌ (0 collector, 0 dữ liệu) sang ⏳ có bằng chứng thật (verify sống
bằng drill sudo-failure trên VM lab `cust-edge`, tới tận `corr:*` Redis + CRAT ADVISORY_DECISION).
3 bug thật tìm được qua drill sống — xem mục Đ49 dưới đây + `docs/audit/invariant_audit_2026-08.md`.)
Đ48 — task #16 (việc gốc phiên trước) **XONG, VERIFY SỐNG bằng build thật #49 SUCCESS** — Jenkins
giờ CHỈ test/build/push Harbor + bump tag git-SHA + commit-back; ArgoCD (`selfHeal: true, prune:
true`, multi-source) là bên DUY NHẤT apply/rollout. Build #49 xác nhận trực tiếp qua `kubectl`:
`omni-fullstack`/`omni-onboarding`/`aoip-provider-portal`/`aoip-tenant-portal`/`omni-gateway` đều
chạy tag thật `e2afaf7`, ArgoCD `Synced Healthy`. Đ47 — migrate Jenkins từ VM systemd vào pod
trong k3s ĐÃ CUTOVER XONG HOÀN TOÀN. Đ46 — build #40 SUCCESS, P0+P1 verify sống bằng incident
thật qua Alertmanager. Đ45 — CI/CD do phiên này đảm nhận hoàn toàn.

## Đ49 — Blueprint hoàn thiện Omni tự vận hành + dọn repo (ĐANG LÀM, chưa xong)

**Bối cảnh:** User yêu cầu rõ: không thêm tính năng mới, chỉ sửa cái sai/xóa cái dư/đồng bộ cái
lệch/bổ sung cái thiếu; đồng thời dọn repo cho gọn ("nhìn nó như bãi rác quá"). Quy ước môi trường
mới chốt: **MacBook = dev, GCP k3s = UAT (KHÔNG phải production)** — luồng bắt buộc: test local
trước → push Gitea → Jenkins build/push Harbor/bump tag → ArgoCD deploy UAT → verify qua kubectl
trên UAT thật. Đã lưu vào memory `project_env_convention_macbook_dev_gcp_uat`.

Blueprint đầy đủ tại `plans/omni-finish-autonomous-sre-and-repo-cleanup-2026-08-10.md` (2 track,
đã review đối kháng Opus trước khi chạy — 6 CRITICAL đã sửa vào plan). Chi tiết bằng chứng từng
bước nằm ở `docs/audit/invariant_audit_2026-08.md` (audit doc liên tục cập nhật theo B0-B6).

### Track A — Repo hygiene: ĐÃ ĐÓNG (commit 1ed174f, b81e380, f9dd56c)
- A1: tách `docs/handoffs/CURRENT_SESSION.md` 3682→316 dòng, archive tại
  `docs/handoffs/archive/SESSION_ARCHIVE_2026-08.md`.
- A2: đối chiếu bảng RETIRED trong CLAUDE.md — 0 file rác sót (đã dọn từ trước).
- A3: xóa `ui/app/` (Next app cũ 19 route, đã retired 2026-07-06), `ui/Dockerfile` root,
  `ui/e2e/`+`ui/playwright.config.ts` (target `svc/omni-ui` đã retired). GIỮ NGUYÊN
  `ui/package.json`/`ui/packages/` — workspace root thật cho 2 portal sống. User đã xác nhận
  trước khi xóa. Ngoài phạm vi: dọn 6/14 worktree sạch trong `.claude/worktrees/` (~600MB, gitignored).

### Track B — Đồng bộ/sửa lệch 9-domain: B0-B6 ĐÃ ĐÓNG + PUSH + DEPLOY, C1 gần xong
- **B0** (commit 89787ed): 8 invariant chính trong CLAUDE.md đối chiếu code+test thật — cả 8 đúng.
- **B1** (commit 9c6fefe, 097239f): **bug thật tái hiện sống trên UAT bằng Admin Simulator**
  (`POST /simulate/sys_hard_fail {target:omni}` qua gateway port-forward) —
  `evidence_mutate_emit.py::emit_hitl_pending()` gửi Kafka+Redis nhưng KHÔNG ghi
  `omni_admin.hitl_decision` (consumer `hitl_dispatcher.py` không đăng ký trong worker loop nào).
  Đã vá: thêm `repo.create_hitl_pending()` trực tiếp tại nguồn, mirror `hitl_telegram.py` đã đúng.
- **B2** (commit 61170a0): vòng học chỉ nhận nhãn khen — ĐÃ FIX TỪ TRƯỚC (commit 383cc1a), không
  cần sửa gì, chỉ xác nhận qua code+test.
- **B3** (commit ff3e6b4): FinGuard→Smart SIEM merge (`plans/finguard-to-smart-siem-merge-2026-08-04.md`)
  CHƯA XONG (S0 dở, S1 collector chưa viết, `omni_admin.playbook`=0 dòng) — KHÔNG tự ý viết S1 vì
  là khối lượng triển khai đáng kể, ranh giới với "tính năng mới" mờ. Domain `security` giữ ❌.
- **B4** (commit 42f3053): domain `hardware` — xác nhận là giới hạn kiến trúc (containerized, 0
  collector), không phải gap môi trường có thể giải quyết bằng đổi hạ tầng. Cập nhật CLAUDE.md.
- **B5** (commit 3428f7f): **bug thật thứ 2** — domain `application` urgency kẹt "medium" vì
  `assess_domain_severity` đọc `error_rate`/`latency_p99_ms` nhưng producer thật
  (`collectors/logs.py`) phát `failed_file_count`/`files_scanned` — lệch bí danh, cùng lớp bug
  `cpu_pct`/`cpu_percent` đã vá năm ngoái ở domain OS. Đã vá + 3 test.
- **B6** (commit a3dc845, deploy qua build #52): quét lại 9 domain tìm bug B5 có lặp ở đâu khác —
  **tìm thấy domain `storage` cùng lỗi hệt B5** (`collectors/storage.py` phát
  `disk_critical_count`/`disk_warn_count`, không phát `disk_pct`/`result`-trong-fact; field
  `disk_percent` mà CLAUDE.md từng ghi thực ra đến từ `collectors/system.py`, cơ chế khác hẳn). Đã
  vá `src/pkg/reasoning/domain_signals.py` + 2 test + cập nhật CLAUDE.md bảng 9-domain. `os_host`/
  `database`/`service`/`network` xác nhận ĐÚNG (không lệch). `kubernetes`/`security`/`hardware`
  ngoài phạm vi quét (cơ chế khác/chưa build).
- **VERIFY SỐNG B1 trên UAT** (sau build #51 SUCCESS, tag `3428f7f`): trigger lại
  `POST /simulate/sys_hard_fail {target:omni}` → trace `sim-sys_hard_fail-604075f2371d` escalate
  `L3_HITL` → `hitl_pending_emitted` → query `omni_admin.hitl_decision` trên `omni-postgres-0`:
  **1 dòng PENDING mới thật** (`pending_id=mut-sim-sys_hard_fail-604075f2371d`,
  `tool_name=human_escalation`, `risk_class=HIGH`) — khác hẳn lần verify B1 đầu tiên (0 dòng,
  trước fix). Fix B1 xác nhận hoạt động đúng trên UAT thật, không chỉ "code đã sửa" suông.
- **C1** (task #11): cập nhật bảng 9-domain trong CLAUDE.md xong (storage/application → ✅ ĐÃ VÁ).
  Còn: build #52 (deploy B6) — kiểm tra kết quả, commit+push phần cập nhật CLAUDE.md/handoff này,
  đóng task list, báo cáo tổng kết cho user.

### Gotcha vận hành mới phát hiện (Đ49) — quan trọng cho phiên sau
1. **Jenkins KHÔNG tự trigger khi push Gitea** (đã biết từ trước, nhắc lại): sau mỗi `git push
   gitea main`, phải tự gọi Jenkins API để trigger build nếu muốn deploy lên UAT ngay. Credential:
   `docs/handoffs/GCP_CREDENTIALS_2026-08-04.md` (không commit) — Jenkins user/pass ở đó. Cách
   trigger qua curl (crumb + cookie jar, xem lịch sử bash trong session này nếu cần lại):
   `curl -c jar -u user:pass .../crumbIssuer/api/json` lấy crumb → `curl -b jar -u user:pass -H
   "Jenkins-Crumb: $CRUMB" -X POST .../job/omni-gcp-deploy/build`.
2. **RACE CONDITION đã gây build #50 FAIL**: trigger Jenkins build RỒI vẫn tiếp tục `git push`
   commit khác lên `gitea` trong lúc build đang chạy → bước "Update image tags in git (GitOps)"
   của Jenkins bị `git push` từ chối (non-fast-forward) vì `main` đã đổi. Image vẫn build/push
   Harbor thành công (không mất gì), chỉ bước tag-bump git fail nên ArgoCD không thấy tag mới.
   **Bài học: sau khi trigger Jenkins build, KHÔNG push thêm commit nào tới khi build xong.** Build
   #51 được trigger lại sau khi ngừng push — xem kết quả ở "Next step" nếu wakeup đã chạy.
3. Bảng `omni_admin` (32 bảng, đúng CLAUDE.md) sống trong schema `omni_admin` của DB `omnidb`
   trên pod `omni-postgres-0` — `psql -U omni -d omnidb` mặc định vào schema `public` (rỗng),
   phải `SELECT ... FROM omni_admin.<table>` tường minh hoặc `\dn`/`SET search_path`.
4. Secret `telegram-bot` (namespace `multi-agent`) trên UAT vẫn là **placeholder rỗng**
   (`bot-token=""`, `chat-id="0"`) + `OMNI_TELEGRAM_ENABLED=false` trong ConfigMap — đây là thiết
   kế cố ý (file `k8s/deployments/telegram-bot-secret.yaml` ghi rõ), KHÔNG phải bug. User hỏi
   trong phiên này, chưa cung cấp bot token thật nên chưa tạo secret thật — cần bot token +
   chat_id thật từ BotFather nếu muốn bật.

### Đ49 tiếp — Telegram bật thật + Merge FinGuard→Smart SIEM (S0-S4) — ĐÃ ĐÓNG

**Telegram**: user xác nhận có bot thật (`@Leader_Agentic_bot`) trong `.env` gốc (gitignored).
Tạo Secret thật trên UAT qua `kubectl` + bật `OMNI_TELEGRAM_ENABLED=true` trong ConfigMap (đã push
git để ArgoCD không tự revert). Verify sống: `telegram_outbound_ok chat_id=-5174042122
message_id=4454` — tin nhắn thật đã gửi.

**Merge FinGuard→Smart SIEM nội bộ** (user: "merge luôn vào omni đi, nó là tính năng có sẵn và
phải có của omni, không phải thêm tính năng mới") — theo đúng
`plans/finguard-to-smart-siem-merge-2026-08-04.md`:

- **S0** (dọn hệ ngoài chết): xóa 7 manifest (`omni-siem-bridge`/`hitl-dispatcher`/
  `evidence-adapter` + production, `finguard-customer-netpol`), xóa code
  (`hitl_dispatcher.py`, `siem_bridge.py`, `evidence_adapter/worker.py`), gỡ 10 Makefile
  target + 3 script + 1 runbook phụ thuộc. Bỏ nhánh "SIEM luôn suggest-only bất kể tier/risk" —
  đi chung ma trận tier×risk như 8 domain khác. Đổi hardcode `siem_source=="finguard"` →
  chấp nhận bất kỳ giá trị không rỗng, canonical mới `"omni_siem"`.
- **S1**: viết `src/remote_agent/collectors/security.py` — collector ĐẦU TIÊN cho domain
  `security` (2 probe: auth_failures qua `lastb`, privilege_escalation qua
  `journalctl _COMM=sudo`). Opt-in `OMNI_AGENT_SECURITY_ENABLED`.
- **S2**: gateway `agent_webhook.py` fan-out evidence domain=security+FAILED sang thêm
  `omni-siem-raw`. Vá luôn gotcha thật: `EvidenceItem` thiếu field `domain` khiến
  `domain_hint` collector tự khai bị Pydantic âm thầm bỏ.
- **S3**: drill thật trên VM lab `cust-edge` (không có sshd nên dùng probe sudo thay vì
  lastb) — tìm và vá **3 bug thật**:
  1. `_parse_sudo_lines` giả định sai định dạng log (bắt nhầm `"pam_unix(sudo:auth)"`
     làm username) — sửa đọc field `user=`/`ruser=` thật.
  2. Gateway gói `omni-siem-raw` bằng double-envelope `{"data": "..."}` giống
     `omni-diagnostic-evidence`, nhưng `decode_kafka_message` (port từ brain-go Go) đọc
     field PHẲNG — mọi message bị drop `missing_id_or_tenant`. Sửa bỏ double-envelope.
  3. Sửa file `.py` trên đĩa VM KHÔNG hot-reload process Python đang chạy — phải
     `systemctl restart` sau mỗi lần sửa, nếu không process cũ chạy code lỗi trong bộ nhớ
     (đã xảy ra thật, để lại rác `corr:ent:staging-sim:user:pam_unix` làm bằng chứng).
  Verify CUỐI: `corr:ent:staging-sim:user:siemdrilltest` (entity đúng trong Redis) +
  CRAT `ADVISORY_DECISION` ghi thật. Chain `omni-siem-chains` CHƯA hình thành (cần ≥2
  nguồn entity liên quan, đúng thiết kế — chưa test). `case_ledger`/`omni_admin.playbook`
  (0 dòng) để lại việc sau.
- **S4**: không cần làm gì thêm trong phạm vi phiên này — `omni_admin.playbook` 0 dòng
  là thiếu dữ liệu vận hành (seed playbook), không phải lỗi code; `PlaybookMatcher` trả
  `None` một cách hợp lệ khi chưa có playbook.

Domain `security` trong CLAUDE.md: ❌ → **⏳ có bằng chứng thật** (không phải ✅ đầy đủ —
xem bảng chi tiết trong `docs/audit/invariant_audit_2026-08.md` mục S3).

Dọn dẹp VM lab: xóa user tạm `siemdrilltest`, xóa file `.bak-s3drill`. Deploy qua build
#53-#56 SUCCESS (S0→S1+S2→fix parser→fix double-envelope, mỗi build verify riêng trước
khi push tiếp — đúng kỷ luật "1 commit = 1 concern" của plan gốc).

**Task list (11 + 8 task S0-S4 = 19 task): TẤT CẢ completed. Đ49 đóng hoàn toàn.**

## Đ48 — Bỏ hẳn `:latest`, Jenkins chỉ build+push, ArgoCD là bên deploy duy nhất — XONG, VERIFY SỐNG build #49 SUCCESS

**Bối cảnh:** Đ47 migrate Jenkins vào k3s xong nhưng vẫn còn nợ việc GỐC của cả phiên (user hỏi từ
đầu: tại sao Harbor/ArgoCD deploy sẵn mà không thực sự dùng để tag/rollout, pipeline vẫn tag
`:latest` + `kubectl rollout restart` tay). User chốt rõ 2 yêu cầu giữa phiên này: (1) "tôi không
chấp nhận việc gán latest, bắt buộc phải đánh version" — bỏ hẳn `:latest`, chỉ dùng git-SHA thật;
(2) "jenkins chỉ làm nhiệm vụ build, sau đó push lên harbor để ArgoCD deploy chứ nhỉ" — tách bạch
CI (Jenkins: test/build/push/tag) và CD (ArgoCD: deploy/rollout), không phải Jenkins tự
`kubectl apply`+`rollout restart` như trước.

### Đã sửa (Jenkinsfile + k8s/gitops/argocd-application.yaml), validated qua
`/pipeline-model-converter/validate` (Jenkins pod thật, PASS) + `kubectl apply --dry-run=client`
(argocd-application.yaml, PASS)

1. **`Build images` / `Push images to Harbor`**: bỏ hẳn mọi `docker build/push ...:latest`. Chỉ
   build/push `$IMAGE_TAG` = `git rev-parse --short HEAD`, ghi 1 lần vào `.image_tag` NGAY ĐẦU
   stage Build (trước khi bất kỳ stage nào commit ngược vào git) — mọi stage sau đọc file này thay
   vì tự `git rev-parse` lại, vì sau khi stage GitOps commit chạy, HEAD đã đổi sang commit MỚI,
   `git rev-parse HEAD` lúc đó sẽ trả sai SHA (không khớp image thật đã build/push).
2. **Stage mới `Update image tags in git (GitOps)`**: sed thay tag `:latest`/SHA cũ → `$IMAGE_TAG`
   trong 6 file (`omni-fullstack.yaml`, `omni-onboarding.yaml`, `aoip-portals.gcp.yaml`,
   `crat-integrity-check-cronjob.gcp.yaml`, `omni-gateway-rollout.yaml`, và có điều kiện
   `aoip-portals-web.yaml` khi `.build_ui` tồn tại), `git commit` + `git push` thẳng lên
   `gitea.cicd.svc.cluster.local` bằng credential Jenkins `gitea-hiendang` (không phải remote
   `origin` đã checkout — dùng URL tường minh kèm token để tránh phụ thuộc credential-helper).
   AN TOÀN không lặp vô hạn: job `omni-gcp-deploy` xác nhận **không có SCM trigger**
   (`<triggers/>` rỗng, xác nhận 2026-08-04, không đổi qua migrate Đ47) — push này không tự kích
   build mới.
3. **`Apply manifests` / `Deploy Argo Rollouts` / `Deploy portals + Dex`**: bỏ hẳn
   `kubectl apply`/`kubectl rollout restart`/patch `restartAt` cho 6 resource ArgoCD giờ quản
   (chỉ còn "tạo nếu chưa tồn tại" — bootstrap cluster mới trước khi ArgoCD tồn tại). Patch
   `restartAt` trên Rollout `omni-gateway` XOÁ HẲN — lý do tồn tại của nó (tag không đổi nên
   `kubectl apply` không tạo ReplicaSet mới) không còn đúng nữa khi mỗi build đều đổi tag thật.
4. **Stage mới `Wait for ArgoCD rollout`**: thay `kubectl rollout status --timeout=180s` từng
   Deployment bằng poll `kubectl get application omni-core -o jsonpath sync.status/health.status`
   sau khi `kubectl patch ... argocd.argoproj.io/refresh=hard`. Timeout nâng lên **300s** (không
   phải 180s) vì Rollout canary của `omni-gateway` có 2 bước `pause: {duration: 60}` cố ý — riêng
   phần chờ đã 120s, 180s không đủ margin cho canary chạy xong thật.
5. **`k8s/gitops/argocd-application.yaml`**: `selfHeal: true, prune: true` (trước `false/false`);
   chuyển `source:` đơn sang `sources:` (multi-source, ArgoCD 2.13 hỗ trợ) — source 1
   `path: k8s/deployments` include 5 file, source 2 `path: k8s/gitops` include
   `omni-gateway-rollout.yaml` (khác thư mục nên cần source riêng).
   `crat-integrity-check-cronjob.gcp.yaml` CỐ Ý không đưa vào ArgoCD (khác thư mục `k8s/jobs/`,
   CronJob tần suất thấp, không đáng thêm source thứ 3) — vẫn được Jenkins bump tag + `kubectl
   apply` trực tiếp như cũ.
6. **`post{failure{}}` — rollback logic tách 2 nhánh theo ai sở hữu resource**: 6 resource giờ do
   ArgoCD `selfHeal` quản lý (`omni-fullstack`, `omni-onboarding`, `aoip-provider-portal`,
   `aoip-tenant-portal`, `aoip-provider-web`, `aoip-tenant-web`, Rollout `omni-gateway`) — `kubectl
   rollout undo`/`kubectl argo rollouts undo` cho các resource này bị XOÁ khỏi rollback cũ vì
   selfHeal sẽ ĐÈ NGƯỢC lại undo đó ngay lần reconcile kế tiếp (undo làm live-state lệch khỏi git
   → selfHeal "sửa" nó về lại đúng cái commit lỗi). Rollback đúng kiểu GitOps: `git revert` chính
   commit tag-bump mà BUILD NÀY vừa push (SHA đọc từ `.gitops_commit_sha`, file này được `rm -f`
   ngay đầu stage Test mỗi build để không bao giờ lỡ revert nhầm commit của build TRƯỚC), rồi push
   — ArgoCD tự hội tụ về tag tốt cuối cùng. aoip-dex + Prometheus/Loki/Mimir/Grafana (không do
   pipeline này tag) vẫn giữ `kubectl rollout undo` như cũ.

### VERIFY SỐNG — build #49, 2026-08-10, SUCCESS (~10.4 phút)

- `Update image tags in git`: sed bump đúng 5 manifest (`omni-fullstack.yaml`,
  `omni-onboarding.yaml`, `aoip-portals.gcp.yaml`, `crat-integrity-check-cronjob.gcp.yaml`,
  `omni-gateway-rollout.yaml`) sang `e2afaf7`, `git commit` (detached HEAD `d964e1a`) + push thẳng
  lên `gitea.cicd.svc.cluster.local` bằng credential `gitea-hiendang` — log console xác nhận
  `e2afaf7..d964e1a HEAD -> main`. `aoip-portals-web.yaml` ĐÚNG THIẾT KẾ không bump lần này (build
  không đổi `ui/`, `.build_ui` không được tạo).
- `Wait for ArgoCD rollout`: patch hard-refresh → poll 35 lần (175s, trong ngưỡng 300s) →
  `sync=Synced health=Healthy`. Thấy rõ `health=Suspended` xen giữa (đúng dự đoán trong comment —
  Argo Rollouts canary `pause: {duration: 60}` của `omni-gateway` báo trạng thái đó qua ArgoCD).
- Verify trực tiếp qua `kubectl` (không suy đoán từ log): `omni-fullstack`, `omni-onboarding`,
  `aoip-provider-portal`, `aoip-tenant-portal`, `omni-gateway` — cả 5 pod đang chạy đều có
  `image: .../*:e2afaf7`, 0 restart. `kubectl get application omni-core` → `Synced Healthy`.
  `kubectl get rollout omni-gateway` → `Healthy`, image đúng tag.
- Local repo đã fetch+fast-forward `d964e1a` từ `gitea`, đã push tiếp sang `origin` (GitHub) —
  cả 2 remote khớp nhau, đúng convention 2 remote của repo này.
- `crat-integrity-check-cronjob.gcp.yaml` (không do ArgoCD track) — Jenkins tự `kubectl apply`
  trực tiếp trong "Deploy OrbStack-parity gaps" như thiết kế, log xác nhận `configured`.

### Còn lại (không chặn, việc phụ)

- `aoip-provider-web`/`aoip-tenant-web` vẫn `:latest` trong git + trên cluster — chờ lần build kế
  tiếp có đổi `ui/` mới chuyển sang tag thật (thiết kế cố ý, không phải nợ).
- CHƯA có cơ hội test đường `post{failure{}}` git-revert thật (build #49 không fail) — để dành khi
  có 1 build fail thật xảy ra tự nhiên, không nên cố tình phá build chỉ để test rollback.
- `CLAUDE.md` phần Jenkins/CI-CD nên cập nhật ngắn cho khớp kiến trúc mới (Jenkins = CI-only,
  ArgoCD = CD) — chưa làm, việc nhỏ.

## Đ47 — Migrate Jenkins vào k3s — ĐÃ CUTOVER XONG, production khoẻ

**Bối cảnh:** user hỏi tại sao Harbor/ArgoCD không thực sự dùng để tag/rollout image (đúng —
pipeline luôn `docker save | k3s ctr images import` + tag `:latest`, `kubectl rollout restart`
tay). Đề xuất ban đầu: tag git-SHA + push Harbor + `set image`. User chọn hẳn phương án lớn hơn:
Jenkins chạy trong pod k3s (network/DNS cluster thật) thay vì VM systemd, làm nền cho GitOps
đầy đủ sau này.

### Đã xong, đang sống
1. **Sự cố ArgoCD `omni-core` tự phát hiện + vá** (không liên quan yêu cầu gốc, tình cờ thấy):
   `ComparisonError("authentication required")` từ 2026-08-09 — Secret `omni-gitea-repo` (ns
   `argocd`) có password RỖNG. Root cause: `Jenkinsfile` cũ suy token từ `git remote get-url
   gitea`, nhưng Jenkins tự checkout bằng remote `origin` + `credentialsId: gitea-hiendang`
   (xác nhận qua `config.xml` thật), KHÔNG có remote `gitea` — lệnh fail, nhưng pipe thẳng vào
   `sed` (không `pipefail`) nên nuốt lỗi âm thầm, ghi Secret rỗng "thành công" mỗi build. Fix:
   patch Secret sống ngay (dùng token local đang hoạt động) + sửa Jenkinsfile dùng
   `withCredentials(gitea-hiendang)` thay vì parse. Verify sống: `Synced Healthy`. Commit
   `bf8df1a` (đã push cả 2 remote).
2. **Harbor admin password reset trực tiếp qua Postgres** — cả Secret `harbor-admin-bootstrap`
   lẫn giá trị trong `GCP_CREDENTIALS_2026-08-04.md` đều SAI (401, log harbor-core xác nhận
   "Invalid credentials" thật, không phải lock/network). User tự cho 1 giá trị nữa — VẪN sai.
   Root cause thật: không rõ (có thể rotate không đồng bộ ở lần security-sweep nào đó). Fix:
   đọc source Harbor v2.15.2 thật (`src/common/utils/encrypt.go` qua GitHub) xác nhận đúng
   scheme `pbkdf2_sha256` = PBKDF2-HMAC-SHA256, **600000 iterations** (không phải 10000 như lần
   đầu tôi đoán sai), dklen=16 byte → hex. Generate password mới + salt mới, `UPDATE harbor_user
   SET password=..., salt=... WHERE username='admin'` trực tiếp trên `harbor-database-0`. Verify
   sống: HTTP 200 `/api/v2.0/users/current`. Password mới đã ghi vào Secret
   `harbor-admin-bootstrap` VÀ `docs/handoffs/GCP_CREDENTIALS_2026-08-04.md` — file này ĐÃ
   tracked trong git từ trước (commit `2b85d4d`, không gitignore — pattern sẵn có của repo,
   không phải tôi đổi), nên commit password mới cùng handoff update này.
3. **Image `jenkins-controller:v1` build + push Harbor thành công.** Dockerfile mới:
   `docker/jenkins-controller/Dockerfile` (base `jenkins/jenkins:lts-jdk21` + kubectl v1.36.2 +
   helm v3.21.3 + istioctl 1.30.3 + docker CLI 29.7.1, khớp đúng version VM cũ — xác nhận qua
   Jenkins Script Console). Digest:
   `sha256:80ac53cdee9210b37cb486a7dd621d029775c72a7fdddee70f99228c983f598e`.
   Gotcha: `docker login` LUÔN thử HTTPS bất kể `insecure-registries` trong `daemon.json`
   (setting đó chỉ áp dụng cho daemon khi PUSH/PULL, không áp dụng cho lệnh `login` — 2 code path
   khác nhau trong Docker CLI) → bypass bằng ghi thẳng `~/.docker/config.json` với
   `{"auths":{"<ip>":{"auth":"<base64 admin:pw>"}}}`, không gọi `docker login`.
4. **PVC + ServiceAccount + ClusterRoleBinding + Deployment (DinD sidecar) đã apply** —
   `k8s/gitops/jenkins-incluster.yaml` (namespace `cicd`). `ClusterRoleBinding` cluster-admin
   (khớp quyền kubeconfig VM cũ đang có, single-tenant box, không scope hẹp hơn). DinD sidecar
   `docker:29-dind` privileged, `--insecure-registry=10.43.239.205` riêng cho sidecar (không cần
   sửa Docker host nữa về lâu dài). hostPort **8081** (KHÔNG phải 8080) — cố ý, để chạy song
   song với Jenkins VM cũ (vẫn đang là kênh Script Console tôi dùng để thao tác VM) cho tới khi
   verify xong mới cutover sang 8080.
5. **Data copy `/var/lib/jenkins` (483.8M) → PVC `jenkins-home` THÀNH CÔNG** — pod tạm
   `busybox` mount `hostPath:/var/lib/jenkins` (readOnly) + PVC, `cp -a`. Đã xác nhận có đủ
   `secrets/`, `credentials.xml`, `jobs/`, `secret.key`, `identity.key.enc` — tức là credential
   `gitea-hiendang` và toàn bộ job `omni-gcp-deploy` NÊN còn nguyên khi Jenkins pod mới đọc PVC
   này (CHƯA verify — xem BLOCKER).

### k3s restart — user tự chạy xong, node/cluster khoẻ
User tự `sudo tee /etc/rancher/k3s/registries.yaml` (mirror HTTP cho Harbor ClusterIP) +
`sudo systemctl restart k3s` trên VM. Verify sau restart: node `Ready`, không pod nào khác trên
cluster bị crash-loop do containerd restart (chỉ có 1 pod `svclb-istio-ingressgateway` Pending
từ trước, không liên quan). An toàn.

### 2 bug hạ tầng phát sinh SAU restart, đã tự phát hiện + vá cùng đợt
6. **UID/GID lệch sau copy** — pod `jenkins` `1/2 CrashLoopBackOff`, log
   `"missing rw permissions on JENKINS_HOME"`. Root cause: data copy (mục 5) giữ nguyên UID/GID
   gốc từ VM (`107:109`, user `jenkins` hệ thống Debian), nhưng image `jenkins/jenkins` chạy
   user `1000:1000`. Fix: `chown -R 1000:1000` toàn bộ PVC qua 1 pod debug tạm (root), xoá pod
   sau khi xong.
7. **DinD tự bật TLS mặc định** — `docker build`/`docker images` (kể cả lệnh không chạm registry)
   lỗi `"Client sent an HTTP request to an HTTPS server"`. Root cause: `docker:dind`'s entrypoint
   tự generate cert + bật TLS qua `DOCKER_TLS_CERTDIR` mặc định `/certs`, BẤT KỂ arg
   `--host=tcp://0.0.0.0:2375` tường minh. Fix: set `env: DOCKER_TLS_CERTDIR=""` trên container
   `dind` — cách chính thức Docker tài liệu hoá để tắt hẳn auto-TLS.
8. **insecure-registry match theo string, không theo IP đã resolve** — push bằng DNS name
   (`harbor.harbor.svc.cluster.local`) vẫn bị coi là HTTPS dù IP `10.43.239.205` đã có trong
   `--insecure-registry`, vì Docker match chuỗi TRƯỚC khi resolve DNS. Fix: liệt kê CẢ HAI dạng
   (IP và DNS name) trong `--insecure-registry` của sidecar `dind`.

### VERIFY SỐNG — đã xác nhận đầy đủ (không chỉ "rollout thành công")
- `kubectl get pods -n cicd -l app=jenkins` → `2/2 Running`.
- `curl http://100.67.117.19:8081/login` → `200`.
- API `/api/json` → job `omni-gcp-deploy` còn nguyên (kèm build history cũ, thấy cả build #42).
- Credential `gitea-hiendang` (Username-password) còn đọc được qua API.
- `kubectl exec ... -- kubectl get ns` → chạy được qua ServiceAccount trong pod (đã bỏ kubeconfig
  cũ trỏ `127.0.0.1:6443` — file đó chỉ đúng khi Jenkins chạy trực tiếp trên node, sai trong pod;
  đã `mv` sang `.bak`, không xoá).
- `getent hosts harbor.harbor.svc.cluster.local` → resolve ra ClusterIP thật — **đây là bằng
  chứng trực tiếp cho mục tiêu gốc của việc migrate** ("thông network", không có được khi Jenkins
  chạy trên VM host).
- `docker build` (qua DinD, pull base image từ docker.io) → thành công.
- `docker push` tới `harbor.harbor.svc.cluster.local/library/...` → chạm đúng Harbor qua HTTP,
  chỉ báo thiếu credential (`no basic auth credentials`) — đúng hành vi kỳ vọng, chưa cấu hình
  login cho test thủ công này, KHÔNG phải lỗi.

### Việc gốc CHƯA làm — Harbor git-SHA tag + ArgoCD selfHeal (task #16)
Đây là câu hỏi ban đầu của user ("CI/CD có full harbor, argocd, phải đánh tag image và cập nhật
trên k3s chứ?") — migrate Jenkins vào cluster chỉ là NỀN TẢNG (network/DNS thật) để làm việc này,
CHƯA phải bản thân việc đó. Hiện tại image vẫn tag `:latest` cố định, `kubectl rollout restart`
tay để force pull. Việc còn lại, session sau làm:
1. Jenkinsfile: tính `IMAGE_TAG=$(git rev-parse --short HEAD)`, build/push CẢ `:latest` (giữ
   tương thích) LẪN `:$IMAGE_TAG` lên Harbor.
2. Cập nhật `image:` trong 6 manifest đã sửa Đ47 (`omni-fullstack.yaml`, `omni-onboarding.yaml`,
   `omni-gateway-rollout.yaml`, `aoip-portals.gcp.yaml`, `aoip-portals-web.yaml`,
   `crat-integrity-check-cronjob.gcp.yaml`) sang tag `$IMAGE_TAG`, `git commit` NGAY TRONG pipeline
   (Jenkins pod đã cluster-admin, chỉ cần `git config user.email/name` + push qua credential
   `gitea-hiendang` đã có sẵn).
3. Bật `selfHeal: true, prune: true` trên Application `omni-core`
   (`k8s/gitops/argocd-application.yaml`) — ArgoCD giờ mới thật sự là nguồn sự thật, không chỉ
   drift-detector. Cân nhắc mở rộng `directory.include` để phủ luôn
   `omni-gateway-rollout.yaml`/portal manifest thay vì chỉ 3 file hiện tại.
4. Bỏ hẳn `kubectl rollout restart` thủ công cho các deployment này — thay đổi tag ảnh tự nhiên
   trigger rollout thật, không cần force nữa.

### Việc khác trong phiên (không liên quan Jenkins migrate)
- ⚠️ **Tự phát hiện + báo ngay:** đầu phiên, 1 lệnh `sed` mask lỗi làm lộ password Jenkins VM
  cleartext trong transcript (regex không khớp format `**Password**:`). Đã báo user, khuyến
  nghị đổi password Jenkins — **VẪN CHƯA XÁC NHẬN user đã đổi hay chưa, nhắc lại đầu phiên sau.**
  (Không còn quan trọng bằng trước vì VM Jenkins đã tắt hẳn, nhưng account `hiendang` trên VM vẫn
  dùng password đó cho SSH/sudo nếu có — vẫn nên đổi.)

### Files changed (Đ47) — ĐÃ COMMIT + PUSH ĐẦY ĐỦ (4 commit, cả gitea+origin)
`bf8df1a` → `c89e8e9` → `3ffa38a` → `fc63a8b`. Không còn gì treo chưa commit từ Đ47.
- `Jenkinsfile`: fix credential ArgoCD (`withCredentials`), build→push Harbor (bỏ hẳn
  `sudo k3s ctr images import`), stage "Push images to Harbor" mới.
- `docker/jenkins-controller/Dockerfile`: image controller (kubectl/helm/istioctl/docker CLI/
  python3/PyYAML) — build thật trên Harbor `10.43.239.205/library/jenkins-controller:v3`.
- `k8s/gitops/jenkins-incluster.yaml`: Deployment 2 container (jenkins+dind) + PVC + RBAC +
  Service NodePort `30080`.
- `k8s/deployments/omni-fullstack.yaml`, `omni-onboarding.yaml`, `aoip-portals.gcp.yaml`,
  `aoip-portals-web.yaml`, `k8s/gitops/omni-gateway-rollout.yaml`,
  `k8s/jobs/crat-integrity-check-cronjob.gcp.yaml`: `image:` trỏ Harbor ClusterIP,
  `imagePullPolicy: Always` (trừ cronjob dùng `IfNotPresent`).
- `docs/handoffs/GCP_CREDENTIALS_2026-08-04.md`: password Harbor mới (đã tracked git từ trước).
- `CLAUDE.md`: cập nhật kiến trúc Jenkins (không còn "systemd trên VM" nữa).

### Verify cutover cuối cùng (đã xác nhận sống, không chỉ tin log)
- Build #48: SUCCESS end-to-end trên Jenkins in-cluster (build→push Harbor→apply→rollout).
- `omni-fullstack`/`omni-onboarding`/`omni-gateway`/portal pods: `2/2 Running`, image field trỏ
  đúng Harbor ClusterIP.
- `omni-gateway` `/healthz` 200, log cho thấy đang xử lý traffic AGENT THẬT
  (`staging-sim_cust-edge/cust-app/cust-db`) — không phải giả lập.
- VM `jenkins.service`: `stop` + `disable`, port 8080 không còn phản hồi (`HTTPCODE:000`).
- Jenkins in-cluster NodePort `:30080`: `200`, pod `2/2 Running`, không crash-loop.
- `kubectl get pods -n multi-agent --field-selector=status.phase!=Running,!=Succeeded`: rỗng —
  không pod nào bất thường sau toàn bộ quá trình.

### Gotcha tổng hợp Đ47 (đọc trước khi động vào Jenkins-in-cluster lần sau)
1. ArgoCD auth broken do bug parse token trong Jenkinsfile cũ (pipe nuốt lỗi, không `pipefail`).
2. Harbor admin password không khớp bất kỳ nguồn lưu trữ nào — phải reset qua Postgres trực tiếp
   (`pbkdf2_sha256`, 600000 iterations, dklen=16 — xem Harbor `src/common/utils/encrypt.go`).
3. `docker login` luôn thử HTTPS bất kể `insecure-registries` — dùng thẳng `~/.docker/config.json`.
4. `insecure-registry` match theo STRING trước khi resolve DNS — cần khai cả IP lẫn DNS name.
5. `docker:dind` tự bật TLS mặc định (`DOCKER_TLS_CERTDIR`) — phải set rỗng tường minh.
6. Copy data giữ nguyên UID/GID gốc — phải `chown` lại khớp UID image mới.
7. kubeconfig copy từ VM trỏ `127.0.0.1:6443` — sai trong pod, xoá để dùng ServiceAccount.
8. Job Jenkins tự cấu hình git URL `localhost:30300` (đúng trên VM, sai trong pod) — sửa qua
   `config.xml` API sang `gitea.cicd.svc.cluster.local:3000`.
9. DinD là container/daemon RIÊNG — bind-mount (`docker build -v $(pwd):/repo`) cần PVC workspace
   mount vào CẢ HAI container, không chỉ container `jenkins`.
10. MTU lồng nhau: pod `eth0` (flannel VXLAN) 1410 vs `docker0` mặc định 1500 — gói lớn bị rớt
    âm thầm (DNS/gói nhỏ vẫn qua được, đánh lừa chẩn đoán ban đầu). Set `--mtu=1400` cho dockerd.
11. Image controller thiếu `python3`/`python3-yaml` — 2 chỗ trong pipeline (`vault-bootstrap.sh`,
    Grafana apply) cần, lỗi rất khác nhau (1 cái âm thầm fallback sai, 1 cái lỗi rõ ràng).

## Đ46 — Verify sống P0+P1 THÀNH CÔNG (build #40), + 1 sự cố tự gây do chính P0 #1 đã vá cùng đợt

**Build #40 SUCCESS** (464s, build #39 trước đó cũng SUCCESS 475s nhưng thiếu code bearer-token —
xem sự cố dưới). `omni-fullstack`/`omni-gateway` đều rolled out, symbol P0/P1 xác nhận có thật
trong pod đang chạy (không chỉ tin "rollout successful"):
- `diagnosis_loop._AGENT_ONLINE_MAX_AGE_S = 150.0`, `_AGENT_REGISTRY_TTL_SEC = 300`
- `evidence_consumer` có `ERR_ALERT_CLASS_READ_FAILED`
- `remote_agent_pipeline.REMOTE_Z_THRESHOLD = 3.0`
- `gateway.api` có `_take_rate_limit_token`, `_MAX_RATE_LIMIT_KEYS=500`, `_verify_webhook_auth`
- `case_ledger.advocacy._MAX_CONCURRENT_PATTERN_FETCHES = 4`

**Verify hành vi sống bằng incident thật** (không chỉ đọc symbol):
- Inject alert `OmniAdvisoryAcceptanceRateLow` qua Alertmanager thật → log xác nhận đúng chuỗi:
  `alert_class=meta_self` → `mutate_eligible=false` → `SUGGEST_REMEDIATION` deterministic,
  KHÔNG qua LLM/mutate, đúng thiết kế P0 #2.
- Unauthenticated POST tới `https://gateway.omnisre.xyz/webhook/prometheus` từ ngoài → **401**
  (đúng, không làm yếu bảo mật P0 #1).
- Alert từ Alertmanager nội bộ (đã có bearer token) → **200** (đường hợp lệ không còn bị chặn).

### Sự cố tự gây — P0 #1 chặn nhầm Alertmanager nội bộ, đã phát hiện+vá+verify cùng session
Sau khi build #39 deploy, `kubectl get ingress` xác nhận `omni-gateway` **thật sự lộ ra Internet**
qua `gateway.omnisre.xyz` (đúng threat model finding #1) — NHƯNG cùng route đó cũng là đường
Alertmanager nội bộ gửi self-monitoring alert, và Alertmanager `webhook_configs` **không có khả
năng tự ký HMAC** (chỉ hỗ trợ bearer token tĩnh qua `authorization.credentials_file`). Fail-closed
đúng ý nhưng chặn nhầm luôn đường hợp lệ duy nhất — mọi alert (kể cả self-monitoring) bị 503 một
thời gian ngắn giữa build #39 và #40.

Fix (commit `6958476`): `_verify_webhook_auth()` (đổi tên từ `_verify_hmac_signature`) chấp nhận
HMAC HOẶC bearer token `OMNI_ALERTMANAGER_WEBHOOK_TOKEN`. Hạ tầng sửa trực tiếp trên cluster:
- `vault kv patch secret/omni-gateway-secret OMNI_ALERTMANAGER_WEBHOOK_TOKEN=...` (giữ nguyên
  `OMNI_GATEWAY_API_KEY`).
- `k8s/gitops/omni-gateway-external-secret.yaml` map thêm key (namespace `multi-agent`).
- **Gotcha phát hiện live**: mount thẳng `omni-gateway-secret` vào pod alertmanager (namespace
  `monitor`) làm pod treo `ContainerCreating` vĩnh viễn — "secret ... not found" — Secret
  **không cross-namespace được**. Fix: ExternalSecret RIÊNG
  `k8s/gitops/alertmanager-webhook-token-external-secret.yaml` (namespace `monitor`), cùng
  property Vault, một nguồn sự thật, 2 Secret ở 2 namespace.

### Còn treo
- P1 #5 (blocking `psutil.cpu_percent`) chạy trên `src/remote_agent/` — process trên VM khách
  hàng, KHÔNG nằm trong cluster GCP này, chưa verify sống (cần deploy riêng lên VM lab, ngoài
  phạm vi build Jenkins hiện tại).
- **P2** (8 mục MEDIUM, đặc biệt #8 RBAC scope + #9 credential_source_of_truth governance) —
  chưa bắt đầu, cần bàn thiết kế trước.
- **CI/CD architecture gap** (user hỏi trực tiếp, chưa làm): Harbor + ArgoCD đã deploy đầy đủ
  nhưng **chưa nối vào luồng deploy thật** — image build xong `docker save | k3s ctr images
  import` thẳng, bỏ qua Harbor hoàn toàn; luôn tag `:latest` nên phải `kubectl rollout restart`
  tay thay vì rollout theo tag/digest đổi; ArgoCD Application `omni-core` cố tình
  `selfHeal:false, prune:false`, chỉ là drift-detector, Jenkins vẫn là nguồn sự thật duy nhất
  cho rollout (ghi rõ trong comment `argocd-application.yaml`, không phải oversight nhưng cũng
  chưa fix). Đề xuất: build → tag git-SHA → push Harbor → ArgoCD sync theo tag — việc lớn, cần
  quyết định riêng.



---

> Lịch sử checkpoint trước Đ45: xem `docs/handoffs/archive/SESSION_ARCHIVE_2026-08.md`.
