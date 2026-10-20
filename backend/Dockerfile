# Ren backend (FastAPI + agent runtime)
FROM python:3.12-slim

ARG INSTALL_BROWSER=true
ARG INSTALL_NODE=true

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && if [ "$INSTALL_NODE" = "true" ]; then apt-get install -y --no-install-recommends nodejs npm; fi \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependencies first, so this slow layer stays cached until pyproject.toml changes: install the project against a
# stub package, then remove the stub (the app runs from source in /app, see CMD).
COPY pyproject.toml ./
RUN mkdir -p app && touch app/__init__.py \
 && pip install --upgrade pip \
 && pip install ".[cloud]" \
 && if [ "$INSTALL_BROWSER" = "true" ]; then pip install ".[browser]" && python -m playwright install --with-deps chromium; fi \
 && pip install uv \
 && pip uninstall -y ren-backend \
 && rm -rf app build ./*.egg-info
# `uv` above provides `uvx` for Python-based stdio MCP servers.

# Application code — code-only changes rebuild in seconds.
COPY app ./app
COPY run.py ./

RUN useradd --create-home --uid 1000 ren && mkdir -p /data && chown -R ren:ren /data /app
USER ren

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/health || exit 1

# One process: the agent runtime (orchestrators, tasks, schedulers) lives in-process.
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8000"]
