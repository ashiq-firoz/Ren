# Ren execution sandbox: runs agent commands (shell, Python, Node.js, git) in their own container.
# Build:  docker build -t ren-sandbox ./sandbox
# Run:    see README.md (needs SANDBOX_TOKEN and the shared /data volume)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# git + Node.js for the git/code tools, tini to reap orphaned processes, util-linux for prlimit.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git nodejs npm ca-certificates curl unzip procps tini util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Unprivileged runtime user. Run the backend container with the same UID/GID so both can write the shared volume.
ARG SANDBOX_UID=1000
ARG SANDBOX_GID=1000
RUN groupadd --gid "${SANDBOX_GID}" sandbox \
    && useradd --uid "${SANDBOX_UID}" --gid "${SANDBOX_GID}" --create-home --shell /bin/bash sandbox \
    && mkdir -p /data/workspaces \
    && chown -R sandbox:sandbox /data

COPY server.py .

USER sandbox
ENV SANDBOX_ROOT=/data/workspaces \
    HOME=/home/sandbox \
    PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"

# One worker only: background jobs live in this process's memory.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-server-header"]
