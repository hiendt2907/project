// Omni GCP CI/CD — build core images and roll out to the single-node k3s
// cluster on this same VM. Scope: gateway + fullstack (role=full) + onboarding.
// Portal/dex/hitl are added in a later pipeline once the public domain (task #6)
// is settled — their OIDC issuer strings are absolute-string-compared and must
// not be guessed ahead of that decision.
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
        '''
      }
    }

    stage('Import into k3s containerd') {
      steps {
        sh '''
          set -e
          docker save multi-agent-system:latest | sudo k3s ctr images import -
          docker save omni-gateway:latest | sudo k3s ctr images import -
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
          kubectl apply -f k8s/deployments/omni-fullstack.yaml
          kubectl apply -f k8s/deployments/omni-onboarding.yaml
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
          kubectl rollout restart deployment/omni-gateway -n multi-agent
          kubectl rollout restart deployment/omni-fullstack -n multi-agent
          kubectl rollout restart deployment/omni-onboarding -n multi-agent
          kubectl rollout status deployment/omni-gateway -n multi-agent --timeout=180s
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
    }
  }
}
