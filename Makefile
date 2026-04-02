# Root Makefile — minimal targets for CI and local evidence.
.PHONY: test-evidence docker-worker deploy-worker e2e-proactive

test-evidence:
	bash scripts/run_test_evidence.sh

docker-worker:
	docker build -t multi-agent-system:latest -f Dockerfile .

deploy-worker:
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker-configmap.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker-rbac.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker.yaml
	./scripts/with_working_kube.sh rollout restart deployment/omni-worker -n multi-agent
	./scripts/with_working_kube.sh rollout status deployment/omni-worker -n multi-agent --timeout=180s

e2e-proactive:
	bash scripts/proactive_e2e.sh
