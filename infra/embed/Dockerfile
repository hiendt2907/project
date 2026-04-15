# CPU embeddings compatible with OpenAI /v1/embeddings (sentence-transformers).
# Use on ARM64 (OrbStack / M4) where vLLM pooling models hit missing _C.rotary_embedding.
FROM python:3.12-slim

ARG DEBIAN_FRONTEND=noninteractive
RUN pip install --no-cache-dir \
        "torch>=2.2.0,<2.7" \
        "sentence-transformers>=3.0.0" \
        "fastapi>=0.115.0" \
        "uvicorn[standard]>=0.32.0"

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
WORKDIR /app
COPY server.py /app/server.py
RUN chown -R appuser:appuser /app
USER appuser

ENV MODEL_ID=nomic-ai/nomic-embed-text-v1.5
EXPOSE 8001
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001"]
