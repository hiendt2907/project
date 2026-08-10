// Omni GCP CI/CD — build core images and roll out to the single-node k3s
// cluster on this same VM. Scope: gateway + fullstack (role=full) + onboarding
// + monitoring + provider/tenant portals + Dex, all on the real public domain
// omnisre.xyz (decided 2026-08-04 — replaces the lab's ai-agent.local, same
// subdomain names). hitl-dispatcher is still deferred to a later pipeline.
pipeline {
  agent any

  environment {
    KUBECONFIG = '/var/lib/jenkins/.kube/config'
  }

  stages {
    stage('Test (pytest gate)') {
      steps {
        sh '''
          set -e
          # Workspace persists across builds — clear any leftover marker from a
          # previous run so a failure here can't be mistaken for "this build
          # already started mutating the cluster" (see Apply manifests / post{failure{}}).
          rm -f .rollout_started
          # Pip cache mounted from the persistent workspace (not the ephemeral
          # container fs) so unchanged deps don't re-download every build — this
          # alone was costing real minutes on every single push regardless of
          # whether requirements*.txt actually changed (build-speed complaint,
          # 2026-08-10).
          #
          # --ignore test_track2a_k8s_sdk.py / test_track2b_diagnostic_proactive.py:
          # these need a live cluster kubeconfig this throwaway container doesn't
          # have — running them here was never going to pass, they were burning
          # 3+ min just to fail on connection-refused before Build images even
          # started (builds #28-35, none of it an actual app-code regression).
          # Covered separately post-deploy against the real cluster, not as a
          # pre-deploy gate on a container with no cluster access.
          #
          # --deselect test_emit_agentic_mutate_if_any_passes_attempt_count_to_emit:
          # same reason as above (needs a real kube-config to reach
          # kubernetes_asyncio's ConfigException path — build #36 confirmed:
          # "Config not found: /home/ciuser/.kube/config"), but it's the only
          # cluster-dependent test in its file so deselecting just this one node
          # instead of --ignoring the whole file keeps the other 9 tests running.
          #
          # procps: provides `ps`, which remote_agent's trusted-binary sandbox
          # tests need on PATH to resolve — python:3.11-slim doesn't ship it,
          # so those tests were BLOCKED (not exercising real logic) every build
          # (build #35: test_remote_agent_command_executor.py env/argv0 KeyErrors).
          #
          # Non-root user: python:3.11-slim runs pytest as root by default, which
          # bypasses filesystem permission bits — test_spool_best_effort_on_
          # unwritable_root (tests/test_remote_agent_outbox.py) asserts graceful
          # degradation when a directory is unwritable, a condition root can
          # never actually hit. Running as an unprivileged user makes the
          # permission the test sets up real again. NOT chowning /repo itself —
          # it's a bind-mount of the actual Jenkins workspace on the VM host;
          # recursively chowning it here would leak out and could break the
          # next build's own git/file ops on the host. pytest only needs to
          # READ /repo (world-readable by default) and writes its own tmp_path
          # fixtures under container-local /tmp for MOST tests — the one
          # exception is dist/ (git-ignored, never checked out by git, so
          # chowning it here can't affect the host's next `git checkout`):
          # test_release_tar_deterministic_and_manifest_has_tar_hash legitimately
          # writes scratch build artifacts there (build #36 confirmed: this test
          # broke the moment pytest stopped running as root, PermissionError on
          # /repo/dist/.tar-hash-check — root had silently bypassed the missing
          # write grant before).
          mkdir -p .pip-cache dist
          docker run --rm -v "$(pwd):/repo" -v "$(pwd)/.pip-cache:/root/.cache/pip" -w /repo python:3.11-slim bash -c "
            apt-get update -qq && apt-get install -y -qq procps >/dev/null &&
            useradd -m ciuser &&
            chmod -R a+r /root/.cache/pip &&
            chown -R ciuser:ciuser /repo/dist &&
            pip install -q -r requirements.txt -r requirements-gateway.txt &&
            su ciuser -c 'PYTHONPATH=src python -m pytest tests/ --ignore=tests/integration --ignore=tests/real_services --ignore=tests/test_track2a_k8s_sdk.py --ignore=tests/test_track2b_diagnostic_proactive.py --deselect tests/test_feedback_full_agentic_planner.py::test_emit_agentic_mutate_if_any_passes_attempt_count_to_emit -q'
          "
        '''
      }
    }

    stage('Security scan') {
      steps {
        sh '''
          set -e
          # Fast per-push gate on the CURRENT tree (not full git history — that's
          # what `make secret-history-audit` already covers separately).
          docker run --rm -v "$(pwd):/repo" zricethezav/gitleaks:v8.18.2 \
            detect --source=/repo --config=/repo/.gitleaks.toml --no-git -v

          mkdir -p .pip-cache
          docker run --rm -v "$(pwd):/repo" -v "$(pwd)/.pip-cache:/root/.cache/pip" -w /repo python:3.11-slim bash -c "
            pip install -q pip-audit &&
            pip-audit -r requirements.txt -r requirements-gateway.txt
          "
        '''
      }
    }

    stage('Build images') {
      steps {
        sh '''
          set -e
          docker build -t multi-agent-system:latest -f Dockerfile .
          docker build -t omni-gateway:latest -f Dockerfile.gateway .

          # Build-speed fix 2026-08-10: the two Next.js portal images were
          # rebuilt (docker build, the single slowest step in this whole
          # pipeline) on EVERY push regardless of whether ui/ changed — a
          # pure-Python commit paid the full Next.js build cost for nothing.
          # Marker file (`.build_ui`) read by the "Import into k3s containerd"
          # and "Deploy portals + Dex" stages below — separate `sh` steps don't
          # share shell state, only the persistent workspace filesystem does
          # (same pattern as `.rollout_started`). `git diff` against HEAD~1
          # falls back to "changed" on any error (shallow clone / first-ever
          # build / no parent commit) — never silently skip a real UI change.
          rm -f .build_ui
          if git diff --name-only HEAD~1 HEAD -- ui/ 2>/dev/null | grep -q . || \
             ! git rev-parse HEAD~1 >/dev/null 2>&1 || \
             ! docker image inspect aoip-provider-web:latest >/dev/null 2>&1 || \
             ! docker image inspect aoip-tenant-web:latest >/dev/null 2>&1; then
            touch .build_ui
            docker build -t aoip-provider-web:latest -f ui/apps/provider-portal/Dockerfile \
              --build-arg AOIP_BACKEND_URL=http://aoip-provider-portal:8081 ui
            docker build -t aoip-tenant-web:latest -f ui/apps/tenant-portal/Dockerfile \
              --build-arg AOIP_BACKEND_URL=http://aoip-tenant-portal:8082 ui
          else
            echo "[skip] ui/ unchanged since HEAD~1 and both portal images already exist locally — skipping Next.js build"
          fi
        '''
      }
    }

    stage('Import into k3s containerd') {
      steps {
        sh '''
          set -e
          docker save multi-agent-system:latest | sudo k3s ctr images import -
          docker save omni-gateway:latest | sudo k3s ctr images import -
          if [ -f .build_ui ]; then
            docker save aoip-provider-web:latest | sudo k3s ctr images import -
            docker save aoip-tenant-web:latest | sudo k3s ctr images import -
          else
            echo "[skip] portal images unchanged this build — already imported from a prior run"
          fi
        '''
      }
    }

    stage('Apply manifests') {
      steps {
        sh '''
          set -e
          # Marks the point past which this build actually starts mutating cluster
          # state — post{failure{}} checks this before attempting any rollback, so
          # a build that fails at Test/Security/Build (never touched the cluster)
          # doesn't undo a perfectly good deploy from a PREVIOUS successful build.
          touch .rollout_started
          kubectl apply -f k8s/deployments/namespace.yaml
          # Single-tenant GCP node: omni-fullstack legitimately needs hostPID/privileged/SYS_ADMIN
          # for its nsenter-based mutate path (OMNI_EXECUTOR_FORCE_NSENTER). The repo's namespace.yaml
          # enforces "baseline" for the shared OrbStack lab; that blocks pod admission here, so this
          # GCP-only pipeline relaxes enforcement on top of the applied manifest.
          kubectl label namespace multi-agent pod-security.kubernetes.io/enforce=privileged --overwrite

          # Bootstrap secret generated once, then reused across builds — same
          # pattern as harbor-admin-bootstrap/grafana-admin. omni-postgres.yaml no
          # longer carries a plaintext Secret (security sweep 2026-08-04 — the old
          # POSTGRES_PASSWORD was committed in cleartext).
          kubectl get secret omni-pg-secret -n multi-agent >/dev/null 2>&1 || {
            PG_PW=$(openssl rand -hex 20)
            kubectl create secret generic omni-pg-secret -n multi-agent \
              --from-literal=POSTGRES_USER=omni \
              --from-literal=POSTGRES_PASSWORD="$PG_PW" \
              --from-literal=POSTGRES_DB=omnidb \
              --from-literal=OMNI_ADMIN_PG_DSN="postgresql://omni:${PG_PW}@omni-postgres.multi-agent.svc.cluster.local:5432/omnidb"
          }
          kubectl apply -f k8s/deployments/omni-postgres.yaml
          kubectl apply -f k8s/deployments/redis-standalone.yaml
          kubectl apply -f k8s/kafka/kafka-single.yaml
          kubectl apply -f k8s/deployments/omni-fullstack-rbac.yaml
          # GCP variant (Ollama reached over Tailscale) — NOT the lab file, see the
          # header comment in omni-worker-configmap.gcp.yaml for why this can't be
          # a partial `kubectl patch` on top of the shared lab ConfigMap.
          kubectl apply -f k8s/deployments/omni-worker-configmap.gcp.yaml
          # k8s/deployments/omni-chaos-secret.yaml removed (security sweep
          # 2026-08-04 — plaintext pg-app-password committed). NOTE: omni-fullstack.yaml
          # still has a required (non-optional) secretKeyRef to omni-chaos-lab/
          # pg-app-password below, so the secret must already exist before that
          # apply runs — bootstrap by hand once per cluster if missing:
          #   kubectl create secret generic omni-chaos-lab -n multi-agent \
          #     --from-literal=pg-app-password="$(openssl rand -hex 16)"
          kubectl apply -f k8s/deployments/telegram-bot-secret.yaml
          kubectl apply -f k8s/deployments/omni-gateway.yaml
          # omni-gateway runs as an Argo Rollouts Rollout instead (see "Deploy
          # Argo Rollouts" stage + k8s/gitops/omni-gateway-rollout.yaml) — the
          # Service/NetworkPolicy from the file above are still real and needed,
          # but the plain Deployment would fight the Rollout over the same
          # `app: omni-gateway` pod selector, so it's deleted right after applying.
          kubectl delete deployment omni-gateway -n multi-agent --ignore-not-found
          kubectl apply -f k8s/deployments/omni-fullstack.yaml
          kubectl apply -f k8s/deployments/omni-onboarding.yaml
        '''
      }
    }

    stage('Install Istio mesh') {
      steps {
        sh '''
          set -e
          # Build-speed fix 2026-08-10: this is one-time cluster bootstrap, not
          # something an app-code push needs to redo. Previously ran unconditionally
          # on EVERY build (istioctl install alone is a real network+diff operation,
          # not "cheap") — skip when istiod is already installed and Available.
          # First-ever deploy on a fresh cluster still bootstraps it, same as before.
          if kubectl get deployment istiod -n istio-system >/dev/null 2>&1 && \
             kubectl wait --for=condition=Available deployment/istiod -n istio-system --timeout=5s >/dev/null 2>&1; then
            echo "[skip] Istio already installed and healthy"
          else
            istioctl install --set profile=default -y
          fi
          kubectl label namespace multi-agent istio-injection=enabled --overwrite
          # omni-gateway.yaml's own NetworkPolicy only allow-lists kafka/redis egress;
          # once the istio-proxy sidecar is injected it also needs istiod (XDS/CA) + DNS,
          # or the sidecar hangs forever at Init with "connection refused" to istiod:15012.
          kubectl apply -f k8s/deployments/omni-gateway-istio-netpol.gcp.yaml
        '''
      }
    }

    stage('Deploy Harbor registry') {
      steps {
        sh '''
          set -e
          kubectl create namespace harbor --dry-run=client -o yaml | kubectl apply -f -
          # Bootstrap secret generated once, then reused across builds so the admin
          # password doesn't rotate on every deploy (same pattern as grafana-admin).
          kubectl get secret harbor-admin-bootstrap -n harbor >/dev/null 2>&1 || \
            kubectl create secret generic harbor-admin-bootstrap -n harbor \
              --from-literal=password="$(openssl rand -hex 16)"
          HARBOR_PW=$(kubectl get secret harbor-admin-bootstrap -n harbor -o jsonpath="{.data.password}" | base64 -d)

          # Build-speed fix 2026-08-10: `helm repo add/update` (network fetch) +
          # `helm upgrade --install` (chart diff against a 10m timeout ceiling) ran
          # on EVERY build regardless of whether harbor-values.yaml changed. Skip
          # when the release is already deployed and harbor-core is Available —
          # re-run picks up real changes to harbor-values.yaml because that edit
          # is exactly the case where harbor-core would need a genuine upgrade
          # (if this guard ever masks a real values change, delete the
          # harbor-core deployment once to force the next build through).
          if helm status harbor -n harbor >/dev/null 2>&1 && \
             kubectl wait --for=condition=Available deployment/harbor-core -n harbor --timeout=5s >/dev/null 2>&1; then
            echo "[skip] Harbor already deployed and healthy"
          else
            helm repo add harbor https://helm.goharbor.io >/dev/null 2>&1 || true
            helm repo update >/dev/null
            helm upgrade --install harbor harbor/harbor -n harbor \
              -f k8s/gitops/harbor-values.yaml \
              --set harborAdminPassword="$HARBOR_PW" \
              --timeout 10m
          fi
        '''
      }
    }

    stage('Deploy ArgoCD (GitOps)') {
      steps {
        sh '''
          set -e
          kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
          # Build-speed fix 2026-08-10: the stock install.yaml apply + Available
          # wait (up to 180s ceiling) ran on EVERY build even though this manifest
          # essentially never changes between app-code pushes. Skip when argocd-
          # server is already installed and Available; a genuine ArgoCD version
          # bump (editing the URL below) naturally busts this guard since the
          # deployment gets replaced and goes briefly unavailable.
          if kubectl get deployment argocd-server -n argocd >/dev/null 2>&1 && \
             kubectl wait --for=condition=Available deployment/argocd-server -n argocd --timeout=5s >/dev/null 2>&1; then
            echo "[skip] ArgoCD already installed and healthy"
          else
            kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.13.2/manifests/install.yaml
            kubectl wait --for=condition=Available deployment -n argocd --all --timeout=180s
          fi

          # This repo has a broken/unreachable git submodule (smart-siem) that isn't
          # needed for anything ArgoCD syncs — disable submodule init so repo-server
          # clones don't fail. `kubectl set env` mutates the live object outside the
          # apply above's last-applied-configuration; doing it via `kubectl apply -f -`
          # of a minimal patch document keeps that annotation consistent so future
          # re-applies of the stock install.yaml don't produce an invalid 3-way merge
          # (hit live: "env[27].valueFrom: may not be specified when value is not
          # empty" after an earlier ad-hoc `kubectl set env`).
          if ! kubectl get deployment argocd-repo-server -n argocd -o jsonpath="{.spec.template.spec.containers[0].env[*].name}" | grep -q ARGOCD_GIT_MODULES_ENABLED; then
            kubectl set env deployment/argocd-repo-server -n argocd ARGOCD_GIT_MODULES_ENABLED=false
            kubectl wait --for=condition=Available deployment/argocd-repo-server -n argocd --timeout=120s
          fi

          # Server needs --insecure so Traefik (not argocd-server's own bundled TLS)
          # terminates HTTPS at the cert-manager-issued ingress cert.
          if ! kubectl get deployment argocd-server -n argocd -o jsonpath="{.spec.template.spec.containers[0].args}" | grep -q -- --insecure; then
            kubectl patch deployment argocd-server -n argocd --type=json \
              -p="[{\\"op\\": \\"add\\", \\"path\\": \\"/spec/template/spec/containers/0/args/-\\", \\"value\\": \\"--insecure\\"}]"
            kubectl rollout status deployment/argocd-server -n argocd --timeout=120s
          fi

          # Stock ArgoCD install.yaml ships per-component NetworkPolicies tuned for
          # multi-tenant clusters; on this k3s node they blocked the application
          # controller from even reaching its own repo-server (confirmed live —
          # "connection error...i/o timeout" on repo-server:8081 until removed).
          # Single-tenant lab/production box: RBAC + auth is the real boundary here.
          kubectl delete networkpolicy --all -n argocd --ignore-not-found

          kubectl apply -f k8s/gitops/argocd-ingress.yaml
        '''

        // Repo credentials for ArgoCD's own repo-server. This used to scrape a
        // token out of `git remote get-url gitea` — that remote name is a LOCAL
        // dev convention (dual-remote: gitea=deploy, origin=GitHub backup); the
        // Jenkins job's own SCM checkout (config.xml) uses remote `origin` with
        // credentialsId `gitea-hiendang` and does NOT embed the token in the URL
        // at all, so that scrape always failed on Jenkins. Its empty stdout piped
        // straight into `sed` (which exits 0 on no-match, no `pipefail`), so the
        // failure was swallowed and this stage happily wrote an empty-password
        // Secret every build. Result found live 2026-08-10: ArgoCD's omni-core
        // Application sat in ComparisonError("authentication required") for over
        // a day before being noticed and hand-patched. Fix: pull the credential
        // straight from the Jenkins credential store Jenkins itself already uses
        // to check this repo out — no parsing, nothing to silently break.
        withCredentials([usernamePassword(credentialsId: 'gitea-hiendang', usernameVariable: 'GITEA_USER', passwordVariable: 'GITEA_TOKEN')]) {
          sh '''
            set -e
            kubectl create secret generic omni-gitea-repo -n argocd \
              --from-literal=type=git \
              --from-literal=url=http://gitea.cicd.svc.cluster.local:3000/hiendang/project.git \
              --from-literal=username="$GITEA_USER" \
              --from-literal=password="$GITEA_TOKEN" \
              --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | kubectl apply -f -
          '''
        }

        sh '''
          set -e
          kubectl apply -f k8s/gitops/argocd-application.yaml
        '''
      }
    }

    stage('Deploy Vault + External Secrets') {
      steps {
        sh '''
          set -e
          # Build-speed fix 2026-08-10: helm repo add/update + 2 chart installs
          # (5m timeout ceiling each) ran on EVERY build. Skip when both are
          # already deployed and healthy — vault-bootstrap.sh below still runs
          # unconditionally every build regardless of this guard, since its job
          # is specifically re-unsealing Vault after a possible VM reboot (see
          # comment below), not (re)installing the chart.
          if kubectl get statefulset vault -n vault >/dev/null 2>&1 && \
             kubectl get deployment -n external-secrets -o name 2>/dev/null | grep -q . && \
             kubectl wait --for=condition=Available deployment -n external-secrets --all --timeout=5s >/dev/null 2>&1; then
            echo "[skip] Vault + External Secrets charts already deployed and healthy"
          else
            helm repo add hashicorp https://helm.releases.hashicorp.com >/dev/null 2>&1 || true
            helm repo add external-secrets https://charts.external-secrets.io >/dev/null 2>&1 || true
            helm repo update >/dev/null

            helm upgrade --install vault hashicorp/vault -n vault --create-namespace \
              --set "server.dataStorage.enabled=true" \
              --set "server.dataStorage.size=5Gi" \
              --set "ui.enabled=true" \
              --timeout 5m
            helm upgrade --install external-secrets external-secrets/external-secrets \
              -n external-secrets --create-namespace --timeout 5m
            kubectl wait --for=condition=Available deployment -n external-secrets --all --timeout=120s
          fi

          kubectl wait --for=condition=PodScheduled pod/vault-0 -n vault --timeout=90s
          bash k8s/gitops/vault-bootstrap.sh

          # Unseal phải tiếp tục sống sau khi pipeline kết thúc: job này không có
          # trigger tự động, nên nếu chỉ dựa vào vault-bootstrap.sh ở trên thì một
          # lần VM reboot là Vault nằm sealed tới lần Build Now kế tiếp (đã xảy ra
          # thật, 2d22h — xem đầu file cronjob).
          kubectl apply -f k8s/gitops/vault-auto-unseal-cronjob.yaml

          kubectl apply -f k8s/gitops/vault-clustersecretstore.yaml
          kubectl apply -f k8s/gitops/omni-gateway-external-secret.yaml
        '''
      }
    }

    stage('Deploy Argo Rollouts') {
      steps {
        sh '''
          set -e
          kubectl create namespace argo-rollouts --dry-run=client -o yaml | kubectl apply -f -
          # Build-speed fix 2026-08-10: same skip-if-already-healthy pattern as
          # Istio/ArgoCD/Vault above — this CRD install essentially never changes
          # between app-code pushes.
          if kubectl get deployment -n argo-rollouts -o name 2>/dev/null | grep -q . && \
             kubectl wait --for=condition=Available deployment -n argo-rollouts --all --timeout=5s >/dev/null 2>&1; then
            echo "[skip] Argo Rollouts controller already installed and healthy"
          else
            kubectl apply -n argo-rollouts -f https://github.com/argoproj/argo-rollouts/releases/latest/download/install.yaml
            kubectl wait --for=condition=Available deployment -n argo-rollouts --all --timeout=120s
          fi

          kubectl apply -f k8s/gitops/omni-gateway-rollout.yaml
          kubectl patch rollout omni-gateway -n multi-agent --type merge \
            -p "{\\"spec\\":{\\"restartAt\\":\\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\\"}}"
        '''
      }
    }

    stage('Deploy portals + Dex (omnisre.xyz)') {
      steps {
        sh '''
          set -e
          # Build-speed fix 2026-08-10: install is genuinely idempotent, but the
          # apply + 180s Available wait still cost real time on every build even
          # when nothing changed. Skip when cert-manager is already healthy.
          if kubectl get deployment -n cert-manager -o name 2>/dev/null | grep -q . && \
             kubectl wait --for=condition=Available deployment -n cert-manager --all --timeout=5s >/dev/null 2>&1; then
            echo "[skip] cert-manager already installed and healthy"
          else
            kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.2/cert-manager.yaml
            kubectl wait --for=condition=Available deployment -n cert-manager --all --timeout=180s
          fi
          kubectl apply -f k8s/deployments/cert-manager-issuer.yaml

          kubectl apply -f k8s/ingress/traefik-middlewares.gcp.yaml
          # Dex client secrets sourced from Vault (bootstrapped once, see
          # "Deploy Vault + External Secrets" stage above) — must land before
          # aoip-dex.gcp.yaml so the Secret it mounts already exists.
          kubectl apply -f k8s/gitops/aoip-dex-external-secret.yaml
          kubectl wait --for=condition=Ready externalsecret/aoip-dex-secret -n multi-agent --timeout=60s

          # Build-speed fix 2026-08-10: aoip-dex runs an off-the-shelf image
          # (ghcr.io/dexidp/dex) this pipeline never rebuilds — restarting it on
          # every unrelated app-code push was pure waste. Only restart when
          # `kubectl apply` actually reports a change (manifest edit, or a
          # rotated Vault-sourced client secret landing via the ExternalSecret
          # above) instead of "unchanged" for every resource in the file.
          DEX_APPLY_OUT=$(kubectl apply -f k8s/deployments/aoip-dex.gcp.yaml)
          echo "$DEX_APPLY_OUT"
          kubectl apply -f k8s/deployments/aoip-portals.gcp.yaml
          kubectl apply -f k8s/deployments/aoip-portals-web.yaml
          kubectl apply -f k8s/ingress/omnisre-gcp.yaml

          if echo "$DEX_APPLY_OUT" | grep -qv unchanged; then
            kubectl rollout restart deployment/aoip-dex -n multi-agent
            kubectl rollout status deployment/aoip-dex -n multi-agent --timeout=180s
          else
            echo "[skip] aoip-dex manifest unchanged — no restart needed"
          fi
          # aoip-provider-portal / aoip-tenant-portal run multi-agent-system:latest
          # (the same backend image "Build images" rebuilds on every push) — their
          # restart stays unconditional, unlike aoip-dex/the web images above.
          kubectl rollout restart deployment/aoip-provider-portal -n multi-agent
          kubectl rollout restart deployment/aoip-tenant-portal -n multi-agent
          kubectl rollout status deployment/aoip-provider-portal -n multi-agent --timeout=180s
          kubectl rollout status deployment/aoip-tenant-portal -n multi-agent --timeout=180s
          if [ -f .build_ui ]; then
            kubectl rollout restart deployment/aoip-provider-web -n multi-agent
            kubectl rollout restart deployment/aoip-tenant-web -n multi-agent
            kubectl rollout status deployment/aoip-provider-web -n multi-agent --timeout=180s
            kubectl rollout status deployment/aoip-tenant-web -n multi-agent --timeout=180s
          else
            echo "[skip] portal web images unchanged this build — no restart needed"
          fi
        '''
      }
    }

    stage('Deploy OrbStack-parity gaps (alertmanager/backup/CRAT/tempo)') {
      steps {
        sh '''
          set -e
          # Ported from the OrbStack lab so GCP has full parity before the lab is
          # retired (task: tắt OrbStack hẳn luôn) — without these, turning OrbStack
          # off would silently lose Postgres backups and CRAT audit-chain integrity
          # checks, both compliance/durability-relevant, not just nice-to-have.
          kubectl apply -f k8s/chaos-test/alertmanager.yaml
          kubectl apply -f k8s/deployments/omni-postgres-backup-cronjob.yaml
          kubectl apply -f k8s/monitor/tempo.yaml
          # GCP variant only differs in serviceAccountName (omni-fullstack, not
          # omni-worker — the lab SA name doesn't exist in omni-fullstack-rbac.yaml).
          kubectl apply -f k8s/jobs/crat-integrity-check-cronjob.gcp.yaml
        '''
      }
    }

    stage('Deploy Vaultwarden + monitoring BasicAuth') {
      steps {
        sh '''
          set -e
          kubectl create namespace vaultwarden --dry-run=client -o yaml | kubectl apply -f -
          kubectl get secret vaultwarden-admin -n vaultwarden >/dev/null 2>&1 || \
            kubectl create secret generic vaultwarden-admin -n vaultwarden \
              --from-literal=admin-token="$(openssl rand -base64 48)"
          kubectl apply -f k8s/gitops/vaultwarden.yaml
          kubectl rollout status deployment/vaultwarden -n vaultwarden --timeout=90s

          # htpasswd hash generated once, then reused — same bootstrap-secret pattern
          # as grafana-admin/harbor-admin-bootstrap. "hiendang" is fixed; only the
          # password is random, printed once here on first generation only.
          kubectl create namespace monitor --dry-run=client -o yaml | kubectl apply -f -
          if ! kubectl get secret monitoring-basicauth -n monitor >/dev/null 2>&1; then
            MONITORING_PW=$(openssl rand -base64 24 | tr -d '/+=')
            HTPASSWD_LINE=$(htpasswd -nbB hiendang "$MONITORING_PW")
            kubectl create secret generic monitoring-basicauth -n monitor --from-literal=users="$HTPASSWD_LINE"
            echo "Generated new monitoring BasicAuth password — save this now, not printed again: $MONITORING_PW"
          fi
          kubectl apply -f k8s/gitops/monitoring-basicauth-ingress.yaml
        '''
      }
    }

    stage('Deploy monitoring') {
      steps {
        sh '''
          set -e
          # Build-speed fix 2026-08-10: none of Prometheus/Loki/Mimir/Grafana are
          # images this pipeline builds — the 4 unconditional restart+180s-wait
          # cycles below ran on EVERY app-code push regardless of whether monitor/
          # manifests changed, needlessly bouncing the whole observability stack
          # (and briefly blinding it) for an unrelated Python commit. `kubectl
          # apply` prints "unchanged" per-resource when nothing differs — capture
          # that output and only restart+wait when it reports a real change. The
          # forced restart still exists (Deployments here use envFrom/ConfigMap
          # refs with no checksum annotation, so a ConfigMap-only edit wouldn't
          # otherwise trigger a rollout) — it's now conditional, not removed.
          kubectl apply -f k8s/monitor/namespace.yaml
          PROM_OUT=$(kubectl apply -f k8s/monitor/prometheus.yaml); echo "$PROM_OUT"
          kubectl apply -f k8s/monitor/node-exporter.yaml
          kubectl apply -f k8s/monitor/kube-state-metrics.yaml
          kubectl apply -f k8s/monitor/promtail.yaml
          LOKI_OUT=$(kubectl apply -f k8s/monitor/loki.yaml); echo "$LOKI_OUT"
          kubectl apply -f k8s/monitor/redis-exporter.yaml
          MIMIR_OUT=$(kubectl apply -f k8s/monitor/mimir.yaml); echo "$MIMIR_OUT"

          # GCP dashboards replace the lab's (grafana-dashboards.yaml /
          # grafana-dashboard-llm.yaml) entirely per explicit request 2026-08-04 —
          # delete-and-rebuild, not layer-on-top. Both ConfigMaps deleted live once;
          # this guards against either ever coming back if some other apply path
          # re-adds them.
          kubectl delete configmap grafana-dashboards grafana-dashboard-omni-llm -n monitor --ignore-not-found
          GRAFANA_OUT=$(kubectl apply -f k8s/monitor/grafana-dashboards.gcp.yaml)
          echo "$GRAFANA_OUT"
          _ALERT_OUT=$(kubectl apply -f k8s/monitor/grafana-alerting-provisioning.yaml)
          echo "$_ALERT_OUT"
          GRAFANA_OUT="$GRAFANA_OUT
$_ALERT_OUT"

          # grafana.yaml bundles the grafana-admin Secret in the same multi-doc file as
          # everything else, still carrying its checked-in __REQUIRED_*__ placeholder.
          # Applying the whole file every build was resetting the real password back to
          # that placeholder each time (kubectl apply's 3-way merge sees the file's
          # tracked-desired state literally has the placeholder, so it "corrects" the
          # live Secret back to it) — confirmed live: password rotated on every single
          # build instead of staying stable. Filter the Secret doc out before applying;
          # it's managed exclusively by the guarded create-if-placeholder step below.
          _GRAFANA_MAIN_OUT=$(python3 -c "
import sys, yaml
with open('k8s/monitor/grafana.yaml') as f:
    docs = [d for d in yaml.safe_load_all(f) if d and not (d.get('kind') == 'Secret' and d['metadata']['name'] == 'grafana-admin')]
yaml.dump_all(docs, sys.stdout)
" | kubectl apply -f -)
          echo "$_GRAFANA_MAIN_OUT"
          GRAFANA_OUT="$GRAFANA_OUT
$_GRAFANA_MAIN_OUT"

          # Real values never live in git; generate/keep out of source control, and only
          # write once so re-runs don't rotate the admin password on every deploy.
          CURRENT_PW=$(kubectl get secret grafana-admin -n monitor -o jsonpath="{.data.password}" 2>/dev/null | base64 -d)
          if [ "$CURRENT_PW" = "__REQUIRED_GRAFANA_ADMIN_PASSWORD__" ] || [ -z "$CURRENT_PW" ]; then
            kubectl create secret generic grafana-admin -n monitor \
              --from-literal=password="$(openssl rand -hex 16)" \
              --dry-run=client -o yaml | kubectl apply -f -
          fi
          # Grafana's own alerting provisioning initContainer hard-fails startup if
          # bot-token is empty ("could not find Bot Token in settings") — unlike the
          # omni-worker telegram-bot secret, this can't be blank even when unused.
          CURRENT_BOT=$(kubectl get secret grafana-telegram-alerting -n monitor -o jsonpath="{.data.bot-token}" 2>/dev/null | base64 -d)
          if [ -z "$CURRENT_BOT" ]; then
            kubectl create secret generic grafana-telegram-alerting -n monitor \
              --from-literal=bot-token='0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' \
              --from-literal=chat-id='0' \
              --dry-run=client -o yaml | kubectl apply -f -
          fi

          if echo "$PROM_OUT" | grep -qv unchanged; then
            kubectl rollout restart statefulset/prometheus -n monitor
            kubectl rollout status statefulset/prometheus -n monitor --timeout=180s
          else
            echo "[skip] prometheus manifest unchanged — no restart needed"
          fi
          if echo "$LOKI_OUT" | grep -qv unchanged; then
            kubectl rollout restart deployment/loki -n monitor
            kubectl rollout status deployment/loki -n monitor --timeout=180s
          else
            echo "[skip] loki manifest unchanged — no restart needed"
          fi
          if echo "$MIMIR_OUT" | grep -qv unchanged; then
            kubectl rollout restart deployment/mimir -n monitor
            kubectl rollout status deployment/mimir -n monitor --timeout=180s
          else
            echo "[skip] mimir manifest unchanged — no restart needed"
          fi
          if echo "$GRAFANA_OUT" | grep -qv unchanged; then
            kubectl rollout restart deployment/grafana -n monitor
            kubectl rollout status deployment/grafana -n monitor --timeout=180s
          else
            echo "[skip] grafana manifests unchanged — no restart needed"
          fi
        '''
      }
    }

    stage('Rollout') {
      steps {
        sh '''
          set -e
          # omni-gateway restarts via the "Deploy Argo Rollouts" stage's restartAt
          # patch, not here — it's a Rollout, and `kubectl rollout restart` only
          # understands the built-in Deployment/DaemonSet/StatefulSet kinds.
          kubectl rollout restart deployment/omni-fullstack -n multi-agent
          kubectl rollout restart deployment/omni-onboarding -n multi-agent
          kubectl rollout status deployment/omni-fullstack -n multi-agent --timeout=180s
          kubectl rollout status deployment/omni-onboarding -n multi-agent --timeout=180s
        '''
      }
    }
  }

  post {
    always {
      sh 'kubectl get pods -n multi-agent -o wide || true'
      sh 'kubectl get pods -n monitor -o wide || true'
      sh 'kubectl get rollout -n multi-agent || true'
      sh 'kubectl get application -n argocd || true'
    }
    failure {
      // Auto-rollback: only for the app-layer Deployments/StatefulSet/Rollout that
      // this pipeline itself restarts (never Helm releases — those aren't
      // rollout-versioned the same way and a bad undo there is a bigger blast
      // radius than the failure being handled). `|| true` on every line: this is a
      // best-effort safety net running inside an already-failed build, so one
      // resource with no prior revision (e.g. first-ever deploy) must not stop the
      // rest of the undo attempts.
      sh '''
        if [ ! -f .rollout_started ]; then
          echo "[rollback] build failed before touching the cluster (Test/Security/Build stage) — nothing to roll back, skipping"
          exit 0
        fi
        echo "[rollback] pipeline failed — attempting kubectl rollout undo on app-layer resources"
        kubectl rollout undo deployment/omni-fullstack -n multi-agent || true
        kubectl rollout undo deployment/omni-onboarding -n multi-agent || true
        kubectl rollout undo deployment/aoip-dex -n multi-agent || true
        kubectl rollout undo deployment/aoip-provider-portal -n multi-agent || true
        kubectl rollout undo deployment/aoip-tenant-portal -n multi-agent || true
        kubectl rollout undo deployment/aoip-provider-web -n multi-agent || true
        kubectl rollout undo deployment/aoip-tenant-web -n multi-agent || true
        kubectl rollout undo statefulset/prometheus -n monitor || true
        kubectl rollout undo deployment/loki -n monitor || true
        kubectl rollout undo deployment/mimir -n monitor || true
        kubectl rollout undo deployment/grafana -n monitor || true
        # Rollout (Argo Rollouts CRD) isn't a builtin kind `kubectl rollout` knows —
        # needs the kubectl-argo-rollouts plugin (installed on this Jenkins host).
        kubectl argo rollouts undo omni-gateway -n multi-agent || true
        echo "[rollback] done — see 'kubectl get pods -n multi-agent -o wide' above for resulting state"
      '''
    }
  }
}
