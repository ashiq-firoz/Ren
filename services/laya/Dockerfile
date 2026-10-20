# Laya (convaiinnovations/laya) — fast, calibrated, non-generative decision model used by Ren as the
# policy-alignment checker. Serves the Jev-compatible API: POST /v1/systemone, /v1/systemone/batch.
#
# CPU build (default). For NVIDIA GPUs build with:  --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu124
# and run with LAYA_DEVICE=cuda (see docker-compose.gpu.yml).
FROM python:3.12-slim

ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ENV PIP_NO_CACHE_DIR=1 \
    USE_TF=0 \
    HF_HOME=/models \
    LAYA_HOST=0.0.0.0 \
    LAYA_PORT=8000 \
    LAYA_PRELOAD=1 \
    LAYA_MODELS=english,multilingual \
    LAYA_THREADS=4

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
 && pip install --index-url "$TORCH_INDEX" torch \
 && pip install "laya[serve]"

VOLUME ["/models"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=5 \
  CMD curl -fsS http://localhost:8000/health || exit 1
CMD ["laya-serve"]
