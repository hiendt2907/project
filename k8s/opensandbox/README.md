# OpenSandbox (Execution Plane) — OrbStack / ARM64

Tham chiếu upstream: [alibaba/OpenSandbox](https://github.com/alibaba/OpenSandbox).

## Trong repo này

- `namespace-quota-netpol.yaml` — namespace, quota, limit, egress + deny ingress (trừ shim).
- `shim-netpol-ingress.yaml` — chỉ `multi-agent` → shim `:8888`.
- `shim-rbac.yaml` — ServiceAccount + Role tạo Job/Pods trong `opensandbox`.
- `shim-deployment.yaml` — Deployment + Service `opensandbox-shim:8888`.

### Bật shim (local / OrbStack)

```bash
docker build -t opensandbox-shim:latest -f Dockerfile.opensandbox-shim .
kubectl apply -f k8s/opensandbox/namespace-quota-netpol.yaml
kubectl apply -f k8s/opensandbox/shim-netpol-ingress.yaml
kubectl apply -f k8s/opensandbox/shim-rbac.yaml
kubectl apply -f k8s/opensandbox/shim-deployment.yaml
kubectl rollout status deployment/opensandbox-shim -n opensandbox --timeout=120s
```

Omni-worker đã set `OMNI_OPENSANDBOX_ENABLED=true` và base URL shim trong `k8s/deployments/omni-worker.yaml`.

Upstream đầy đủ: [alibaba/OpenSandbox](https://github.com/alibaba/OpenSandbox) (thay shim bằng operator khi cần).

## RBAC

Omni-worker **chỉ** gọi HTTP tới OpenSandbox API; không cần quyền tạo Pod sandbox trực tiếp. Runtime OpenSandbox dùng ServiceAccount riêng theo manifest upstream.
