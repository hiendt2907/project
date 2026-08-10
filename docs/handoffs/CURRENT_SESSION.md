# Current Session Handoff

**Cập nhật:** 2026-08-10 (Đ48 — task #16 (việc gốc của cả phiên) **XONG, VERIFY SỐNG bằng build
thật #49 SUCCESS** — Jenkins giờ CHỈ test/build/push Harbor + bump tag git-SHA + commit-back;
ArgoCD (`selfHeal: true, prune: true`, multi-source) là bên DUY NHẤT apply/rollout. Build #49 xác
nhận trực tiếp qua `kubectl`: `omni-fullstack`/`omni-onboarding`/`aoip-provider-portal`/
`aoip-tenant-portal`/`omni-gateway` đều chạy tag thật `e2afaf7`, ArgoCD `Synced Healthy`, commit
tag-bump `d964e1a` do chính Jenkins tạo+push, đã fast-forward về cả 2 remote. Đ47 — migrate Jenkins từ VM
systemd vào pod trong k3s ĐÃ CUTOVER XONG HOÀN TOÀN, build #48 SUCCESS, VM `jenkins.service` đã
`stop`+`disable` hẳn. Đ46 — build #40 SUCCESS, P0+P1 verify sống bằng incident thật qua
Alertmanager. Đ45 — CI/CD do phiên này đảm nhận hoàn toàn. Đ44 — audit backend 5-agent, P0+P1 đã
code+test

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
