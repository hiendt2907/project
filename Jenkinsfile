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
    stage('Build images') {
      steps {
        sh '''
          set -e
          docker build -t multi-agent-system:latest -f Dockerfile .
          docker build -t omni-gateway:latest -f Dockerfile.gateway .
          docker build -t aoip-provider-web:latest -f ui/apps/provider-portal/Dockerfile \
            --build-arg AOIP_BACKEND_URL=http://aoip-provider-portal:8081 ui
          docker build -t aoip-tenant-web:latest -f ui/apps/tenant-portal/Dockerfile \
            --build-arg AOIP_BACKEND_URL=http://aoip-tenant-portal:8082 ui
        '''
      }
    }

    stage('Import into k3s containerd') {
      steps {
        sh '''
          set -e
          docker save multi-agent-system:latest | sudo k3s ctr images import -
          docker save omni-gateway:latest | sudo k3s ctr images import -
          docker save aoip-provider-web:latest | sudo k3s ctr images import -
          docker save aoip-tenant-web:latest | sudo k3s ctr images import -
        '''
      }
    }

    stage('Apply manifests') {
      steps {
        sh '''
          set -e
          kubectl apply -f k8s/deployments/namespace.yaml
          # Single-tenant GCP node: omni-fullstack legitimately needs hostPID/privileged/SYS_ADMIN
          # for its nsenter-based mutate path (OMNI_EXECUTOR_FORCE_NSENTER). The repo's namespace.yaml
          # enforces "baseline" for the shared OrbStack lab; that blocks pod admission here, so this
          # GCP-only pipeline relaxes enforcement on top of the applied manifest.
          kubectl label namespace multi-agent pod-security.kubernetes.io/enforce=privileged --overwrite
          kubectl apply -f k8s/deployments/omni-postgres.yaml
          kubectl apply -f k8s/deployments/redis-standalone.yaml
          kubectl apply -f k8s/kafka/kafka-single.yaml
          kubectl apply -f k8s/deployments/omni-fullstack-rbac.yaml
          # GCP variant (Ollama reached over Tailscale) — NOT the lab file, see the
          # header comment in omni-worker-configmap.gcp.yaml for why this can't be
          # a partial `kubectl patch` on top of the shared lab ConfigMap.
          kubectl apply -f k8s/deployments/omni-worker-configmap.gcp.yaml
          kubectl apply -f k8s/deployments/omni-chaos-secret.yaml
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
          istioctl install --set profile=default -y
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
          helm repo add harbor https://helm.goharbor.io >/dev/null 2>&1 || true
          helm repo update >/dev/null

          kubectl create namespace harbor --dry-run=client -o yaml | kubectl apply -f -
          # Bootstrap secret generated once, then reused across builds so the admin
          # password doesn't rotate on every deploy (same pattern as grafana-admin).
          kubectl get secret harbor-admin-bootstrap -n harbor >/dev/null 2>&1 || \
            kubectl create secret generic harbor-admin-bootstrap -n harbor \
              --from-literal=password="$(openssl rand -hex 16)"
          HARBOR_PW=$(kubectl get secret harbor-admin-bootstrap -n harbor -o jsonpath="{.data.password}" | base64 -d)

          helm upgrade --install harbor harbor/harbor -n harbor \
            -f k8s/gitops/harbor-values.yaml \
            --set harborAdminPassword="$HARBOR_PW" \
            --timeout 10m
        '''
      }
    }

    stage('Deploy ArgoCD (GitOps)') {
      steps {
        sh '''
          set -e
          kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
          kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.13.2/manifests/install.yaml

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
          fi
          kubectl wait --for=condition=Available deployment -n argocd --all --timeout=180s

          # Server needs --insecure so Traefik (not argocd-server's own bundled TLS)
          # terminates HTTPS at the cert-manager-issued ingress cert.
          if ! kubectl get deployment argocd-server -n argocd -o jsonpath="{.spec.template.spec.containers[0].args}" | grep -q -- --insecure; then
            kubectl patch deployment argocd-server -n argocd --type=json \
              -p="[{\\"op\\": \\"add\\", \\"path\\": \\"/spec/template/spec/containers/0/args/-\\", \\"value\\": \\"--insecure\\"}]"
          fi
          kubectl rollout status deployment/argocd-server -n argocd --timeout=120s

          # Stock ArgoCD install.yaml ships per-component NetworkPolicies tuned for
          # multi-tenant clusters; on this k3s node they blocked the application
          # controller from even reaching its own repo-server (confirmed live —
          # "connection error...i/o timeout" on repo-server:8081 until removed).
          # Single-tenant lab/production box: RBAC + auth is the real boundary here.
          kubectl delete networkpolicy --all -n argocd --ignore-not-found

          kubectl apply -f k8s/gitops/argocd-ingress.yaml

          # Repo credentials: reuse the same Gitea token this Jenkinsfile pushes with,
          # read from the git remote so it never needs to be typed into a manifest.
          GITEA_TOKEN=$(git remote get-url gitea | sed -n "s#http://[^:]*:\\([^@]*\\)@.*#\\1#p")
          kubectl create secret generic omni-gitea-repo -n argocd \
            --from-literal=type=git \
            --from-literal=url=http://gitea.cicd.svc.cluster.local:3000/hiendang/project.git \
            --from-literal=username=hiendang \
            --from-literal=password="$GITEA_TOKEN" \
            --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | kubectl apply -f -

          kubectl apply -f k8s/gitops/argocd-application.yaml
        '''
      }
    }

    stage('Deploy Vault + External Secrets') {
      steps {
        sh '''
          set -e
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

          kubectl wait --for=condition=PodScheduled pod/vault-0 -n vault --timeout=90s
          bash k8s/gitops/vault-bootstrap.sh

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
          kubectl apply -n argo-rollouts -f https://github.com/argoproj/argo-rollouts/releases/latest/download/install.yaml
          kubectl wait --for=condition=Available deployment -n argo-rollouts --all --timeout=120s

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
          # cert-manager install is idempotent (kubectl apply of the same manifest);
          # cheap to re-run every build so a fresh cluster bootstraps in one pipeline run.
          kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.2/cert-manager.yaml
          kubectl wait --for=condition=Available deployment -n cert-manager --all --timeout=180s
          kubectl apply -f k8s/deployments/cert-manager-issuer.yaml

          kubectl apply -f k8s/ingress/traefik-middlewares.gcp.yaml
          kubectl apply -f k8s/deployments/aoip-dex.gcp.yaml
          kubectl apply -f k8s/deployments/aoip-portals.gcp.yaml
          kubectl apply -f k8s/deployments/aoip-portals-web.yaml
          kubectl apply -f k8s/ingress/omnisre-gcp.yaml

          kubectl rollout restart deployment/aoip-dex -n multi-agent
          kubectl rollout restart deployment/aoip-provider-portal -n multi-agent
          kubectl rollout restart deployment/aoip-tenant-portal -n multi-agent
          kubectl rollout restart deployment/aoip-provider-web -n multi-agent
          kubectl rollout restart deployment/aoip-tenant-web -n multi-agent
          kubectl rollout status deployment/aoip-dex -n multi-agent --timeout=180s
          kubectl rollout status deployment/aoip-provider-portal -n multi-agent --timeout=180s
          kubectl rollout status deployment/aoip-tenant-portal -n multi-agent --timeout=180s
          kubectl rollout status deployment/aoip-provider-web -n multi-agent --timeout=180s
          kubectl rollout status deployment/aoip-tenant-web -n multi-agent --timeout=180s
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
          kubectl apply -f k8s/monitor/namespace.yaml
          kubectl apply -f k8s/monitor/prometheus.yaml
          kubectl apply -f k8s/monitor/node-exporter.yaml
          kubectl apply -f k8s/monitor/kube-state-metrics.yaml
          kubectl apply -f k8s/monitor/promtail.yaml
          kubectl apply -f k8s/monitor/loki.yaml
          kubectl apply -f k8s/monitor/redis-exporter.yaml
          kubectl apply -f k8s/monitor/grafana-dashboards.yaml
          kubectl apply -f k8s/monitor/grafana-dashboard-llm.yaml
          kubectl apply -f k8s/monitor/grafana-alerting-provisioning.yaml

          # grafana.yaml bundles the grafana-admin Secret in the same multi-doc file as
          # everything else, still carrying its checked-in __REQUIRED_*__ placeholder —
          # apply it first, then overwrite just that Secret with a real value.
          kubectl apply -f k8s/monitor/grafana.yaml

          # Real values never live in git; generate/keep out of source control, and only
          # write once so re-runs don't rotate the admin password on every deploy.
          CURRENT_PW=$(kubectl get secret grafana-admin -n monitor -o jsonpath="{.data.password}" | base64 -d)
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

          kubectl rollout restart statefulset/prometheus -n monitor
          kubectl rollout restart deployment/loki -n monitor
          kubectl rollout restart deployment/grafana -n monitor
          kubectl rollout status statefulset/prometheus -n monitor --timeout=180s
          kubectl rollout status deployment/loki -n monitor --timeout=180s
          kubectl rollout status deployment/grafana -n monitor --timeout=180s
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
  }
}
