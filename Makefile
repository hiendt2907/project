# Root Makefile — minimal targets for CI and local evidence.
.PHONY: test-evidence docker-worker deploy-worker deploy-worker-legacy deploy-kafka deploy-prober-rbac ensure-kafka-topics e2e-proactive

test-evidence:
	bash scripts/run_test_evidence.sh

docker-worker:
	docker build -t multi-agent-system:latest -f Dockerfile .

# Master Plan V3: omni-prober (alerts+diagnostic) + omni-analyst (evidence) + omni-core (periodic/proactive).
deploy-worker:
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker-configmap.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker-rbac.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/prober-rbac.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/analyst-rbac.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-prober.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-analyst.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-core.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-executor.yaml
	./scripts/with_working_kube.sh rollout restart deployment/omni-prober deployment/omni-analyst deployment/omni-core deployment/omni-executor -n multi-agent
	./scripts/with_working_kube.sh rollout status deployment/omni-prober -n multi-agent --timeout=180s
	./scripts/with_working_kube.sh rollout status deployment/omni-analyst -n multi-agent --timeout=180s
	./scripts/with_working_kube.sh rollout status deployment/omni-core -n multi-agent --timeout=180s
	./scripts/with_working_kube.sh rollout status deployment/omni-executor -n multi-agent --timeout=180s

# Single-process legacy: OMNI_WORKER_ROLE=full (monolith). Scale omni-prober/analyst/core to 0 if using this.
deploy-worker-legacy:
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker-configmap.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker-rbac.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker.yaml
	./scripts/with_working_kube.sh rollout restart deployment/omni-worker -n multi-agent
	./scripts/with_working_kube.sh rollout status deployment/omni-worker -n multi-agent --timeout=180s

deploy-kafka:
	./scripts/with_working_kube.sh apply -f k8s/kafka/kafka-single.yaml

deploy-prober-rbac:
	./scripts/with_working_kube.sh apply -f k8s/deployments/prober-rbac.yaml

ensure-kafka-topics:
	bash scripts/kafka_ensure_omni_topics.sh

e2e-proactive:
	bash scripts/proactive_e2e.sh
