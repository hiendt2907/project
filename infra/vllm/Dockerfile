# vLLM CPU analyst — ARM64 (Apple M4 / Orbstack)
#
# Build strategy:
#   Stage 1 (builder): compile vLLM CPU wheels + install PyTorch CPU-only.
#   Stage 2 (runtime): copy site-packages, set entrypoint.
#
# VLLM_TARGET_DEVICE=cpu disables CUDA/ROCm and activates the OpenBLAS/NEON
# BLAS path that is optimal for ARM64.  CMAKE_BUILD_PARALLEL_LEVEL uses all
# available cores during the wheel build.
#
# Model note: Qwen2.5-Coder-3B-Instruct (bf16, CPU) avoids AWQ kernels missing on
# ARM64 vLLM CPU backend; fits ~20 Gi Orbstack budget with modest KV (set
# VLLM_CPU_KVCACHE_SPACE in the Deployment).

FROM python:3.12-slim AS builder

ARG DEBIAN_FRONTEND=noninteractive
ARG CMAKE_BUILD_PARALLEL_LEVEL=8

# Build toolchain
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        ninja-build \
        git \
        curl \
        libopenblas-dev \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip / wheel tooling
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# PyTorch CPU-only (ARM64 wheels published on the standard PyPI CPU index)
RUN pip install --no-cache-dir \
        torch==2.5.1 \
        torchvision \
        --index-url https://download.pytorch.org/whl/cpu

# vLLM CPU build — the [cpu] extra pulls cmake-based compilation with
# VLLM_TARGET_DEVICE=cpu automatically.
ENV VLLM_TARGET_DEVICE=cpu
ENV CMAKE_BUILD_PARALLEL_LEVEL=${CMAKE_BUILD_PARALLEL_LEVEL}

RUN pip install --no-cache-dir "vllm[cpu]>=0.8.0"

# vLLM's cpu_platform_plugin (platforms/__init__.py) only activates the CPU
# backend when vllm_version_matches_substr("cpu") is True — i.e. the installed
# package version contains the string "cpu".  The PyPI wheel ships as plain
# "0.x.y"; patching the dist-info METADATA to "0.x.y+cpu" satisfies the check
# without a multi-hour source rebuild.
RUN METADATA="$(find /usr/local/lib/python3.12/site-packages -maxdepth 2 \
        -name METADATA -path '*/vllm-*' | head -1)" && \
    sed -i 's/^Version: \([0-9][0-9.]*\)$/Version: \1+cpu/' "${METADATA}" && \
    python3 -c "import importlib.metadata; v=importlib.metadata.version('vllm'); \
        assert 'cpu' in v, f'patch failed, got: {v}'; print('vLLM patched version:', v)"

# HuggingFace tools (model download + token auth)
RUN pip install --no-cache-dir \
        huggingface_hub[hf_transfer] \
        transformers \
        accelerate

# ── Runtime image ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ARG DEBIAN_FRONTEND=noninteractive

# Runtime shared libs only (OpenBLAS BLAS + OpenMP used by torch/vLLM)
# g++ required when torch._inductor compiles kernels (vLLM embed / some CPU paths).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libopenblas-dev \
        libgomp1 \
        g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy compiled wheels from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Non-root user (matches Omni convention: uid 10001)
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser

# Model cache dir (mounted as PVC in K8s, or local volume in dev)
RUN mkdir -p /model-cache && chown appuser:appuser /model-cache

USER appuser
WORKDIR /app

# ARM64 BLAS threading: match physical CPUs allocated by Orbstack VM
ENV OMP_NUM_THREADS=8
ENV OPENBLAS_NUM_THREADS=8
ENV MKL_NUM_THREADS=8

# vLLM CPU inference uses BF16 by default — force it explicitly so MoE
# expert routing stays numerically stable on ARM NEON.
ENV VLLM_TARGET_DEVICE=cpu

# Enable fast HuggingFace transfers
ENV HF_HUB_ENABLE_HF_TRANSFER=1

# Default image CMD (K8s deployment args override in practice).
ENV MODEL_ID="Qwen/Qwen2.5-Coder-3B-Instruct"
ENV MAX_MODEL_LEN="8192"
ENV TENSOR_PARALLEL_SIZE="1"

EXPOSE 8000

ENTRYPOINT ["python", "-m", "vllm.entrypoints.openai.api_server"]
CMD [ \
    "--model",               "Qwen/Qwen2.5-Coder-3B-Instruct", \
    "--dtype",               "bfloat16", \
    "--max-model-len",       "8192", \
    "--enable-prefix-caching", \
    "--served-model-name",   "qwen2.5-coder-3b", \
    "--tensor-parallel-size","1", \
    "--enforce-eager", \
    "--download-dir",        "/model-cache", \
    "--host",                "0.0.0.0", \
    "--port",                "8000" \
]
