# Golden path — split topology (redirect)

**Canonical architecture + ops (một nguồn):** [OMNI_PROJECT_CANONICAL.md](OMNI_PROJECT_CANONICAL.md)

File này giữ lại làm **bookmark** và URL cũ. Nội dung chi tiết (deploy, log, feedback, corpus, Grafana, gates) đã gộp vào `OMNI_PROJECT_CANONICAL.md` — đừng nhân đôi bản dài ở đây.

**Lệnh nhanh (trích từ canonical):**

```bash
make docker-worker && make deploy-worker
make docker-gateway && make deploy-gateway
make ensure-kafka-topics
```

CI đầy đủ: [.cursor/rules/omni-cicd-k8s.mdc](../../.cursor/rules/omni-cicd-k8s.mdc).
