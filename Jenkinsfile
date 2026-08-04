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
          kubectl apply -f k8s/deployments/omni-worker-configmap.yaml
          kubectl apply -f k8s/deployments/omni-chaos-secret.yaml

          # telegram-bot is not wired up in this environment; chat-id must still be a
          # valid integer or WorkerSettings fails pydantic validation at startup.
          kubectl create secret generic telegram-bot -n multi-agent \
            --from-literal=bot-token='' --from-literal=chat-id='0' \
            --dry-run=client -o yaml | kubectl apply -f -

          kubectl apply -f k8s/deployments/omni-gateway.yaml
          kubectl apply -f k8s/deployments/omni-fullstack.yaml
          kubectl apply -f k8s/deployments/omni-onboarding.yaml

          # Ollama stays on the Mac — point workers at it via Tailscale.
          kubectl patch configmap omni-worker-config -n multi-agent --type merge \
            -p '{"data":{"OMNI_VLLM_BASE_URL":"http://100.93.3.96:11434/v1","OMNI_OLLAMA_BASE_URL":"http://100.93.3.96:11434/v1"}}'
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
    }
  }
}
