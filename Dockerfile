# multi-agent-system:latest — non-root per .cursorrules; kubectl for LAB big-bang ingest
FROM python:3.13-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# kubectl (linux arch) — in-cluster auth via serviceaccount token
ARG KUBECTL_VERSION=v1.30.5
RUN ARCH=$(dpkg --print-architecture) \
    && case "$ARCH" in amd64) KARCH=amd64 ;; arm64) KARCH=arm64 ;; *) KARCH=amd64 ;; esac \
    && curl -fsSL -o /tmp/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${KARCH}/kubectl" \
    && install -m 0755 /tmp/kubectl /usr/local/bin/kubectl \
    && rm -f /tmp/kubectl

RUN useradd -m -u 10001 --shell /bin/bash appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
COPY config/ /app/config/
COPY data/ /app/data/
COPY scripts/ /app/scripts/
COPY migrations/ /app/migrations/
RUN chown -R appuser:appuser /app/scripts

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

USER appuser

CMD ["python", "-m", "workers"]
