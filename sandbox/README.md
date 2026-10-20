# Ren execution sandbox

A small FastAPI service that runs the commands Ren's agents ask for (`shell_*`, `code_*`, `git_*` tools) in a
separate container instead of on the backend host. When the backend has `SANDBOX_URL` set, all of its command
execution goes here and code-execution tools drop from **high** to **medium** risk.

The backend and the sandbox mount the **same volume at `/data`** (backend `DATA_DIR=/data`), so the absolute workspace
paths the backend computes (`/data/workspaces/<org>/users/<user>/...`) are valid in both containers.

## API

All endpoints except `/health` need `Authorization: Bearer $SANDBOX_TOKEN`.

| Method | Path | Body / query | Result |
|---|---|---|---|
| `GET` | `/health` | | `{ok, version, auth, root_ready, tools, running_jobs}` |
| `POST` | `/exec` | `{command, cwd, timeout, env, stdin?, roots?, path_prepend?, max_output?}` | `{exit_code, stdout, stderr, duration_ms, truncated, timed_out}` |
| `POST` | `/python` | `{code, cwd, timeout, env, roots?, path_prepend?, max_output?}` | same as `/exec` |
| `POST` | `/jobs` | `{command, cwd, timeout, env, owner, cwd_label?, roots?, path_prepend?}` | job summary `{job_id, status, ...}` |
| `GET` | `/jobs` | `?owner=` | `{jobs: [...]}` |
| `GET` | `/jobs/{id}` | `?owner=&max_chars=&all=` | summary + `output` produced since the previous poll |
| `DELETE` | `/jobs/{id}` | `?owner=` | final summary + `output_tail` |

* `command` is a shell line (string, run with `/bin/bash -c`) or an argv list (executed directly, no shell).
* `cwd` must be absolute and resolve inside `SANDBOX_ROOT`; when `roots` is given (the backend sends the caller's
  private workspace and the org's shared drive) it must also be inside one of them. Anything else is rejected (403).
* `/python` feeds the code through stdin (`python -u -`), so the working directory stays importable.
* Jobs are scoped by `owner` (the backend sends `<org_id>:<user_id>`); other owners get 404.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SANDBOX_TOKEN` | — | Shared secret; **required** (requests are refused with 503 when unset) |
| `SANDBOX_INSECURE` | `0` | `1` disables auth (local development only) |
| `SANDBOX_ROOT` | `/data/workspaces` | Only directories inside this root can be used as `cwd` |
| `SANDBOX_MAX_TIMEOUT` | `600` | Max seconds for `/exec` and `/python` |
| `SANDBOX_MAX_JOB_TIMEOUT` | `14400` | Max lifetime of a background job |
| `SANDBOX_MAX_OUTPUT` | `30000` | Max characters kept per stream (head + tail) |
| `SANDBOX_MAX_JOBS` / `SANDBOX_MAX_JOBS_PER_OWNER` | `32` / `5` | Concurrent background jobs |
| `SANDBOX_MAX_FILE_MB` | `1024` | `RLIMIT_FSIZE` per process (via `prlimit`) |
| `SANDBOX_MAX_PROCS` | `0` (off) | `RLIMIT_NPROC` per process; prefer the container `pids_limit` |
| `SANDBOX_MAX_MEMORY_MB` | `0` (off) | `RLIMIT_AS` per process; prefer the container memory limit |
| `SANDBOX_THREADS` | `64` | Worker threads (max concurrent `/exec` + `/python` requests) |

Backend side: `SANDBOX_URL=http://sandbox:8080` and the same `SANDBOX_TOKEN`.

## Docker Compose example

```yaml
services:
  backend:
    image: ren-backend
    user: "1000:1000"                 # same UID as the sandbox user, so both can write /data
    environment:
      DATA_DIR: /data
      SANDBOX_URL: http://sandbox:8080
      SANDBOX_TOKEN: ${SANDBOX_TOKEN}
    volumes: [ren-data:/data]

  sandbox:
    build: ./sandbox
    environment:
      SANDBOX_TOKEN: ${SANDBOX_TOKEN}
    volumes: [ren-data:/data]
    # never publish the port: only the backend should reach it
    read_only: true
    tmpfs: [/tmp]
    mem_limit: 2g
    cpus: 2
    pids_limit: 512
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]

volumes:
  ren-data:
```

Generate a token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Security model

* **Authentication**: bearer token compared in constant time; without a token every execution request is refused.
* **Filesystem scope**: working directories are confined to `SANDBOX_ROOT` and the caller's roots; the backend also
  resolves every path through its workspace sandbox before calling.
* **Processes**: each command gets its own process group, stdin closed (unless input is supplied), a wall-clock
  timeout that kills the whole group, and capped output. `prlimit` disables core dumps and caps file sizes.
  `tini` reaps orphaned processes.
* **Environment**: children receive a minimal environment plus the backend-provided variables (`REN_*`, a per-user
  `HOME` and `TMPDIR` inside the workspace); the sandbox's own variables (including `SANDBOX_TOKEN`) are not passed
  on, and loader/shell hooks such as `LD_PRELOAD` or `BASH_ENV` are dropped.
* **User**: the service and all commands run as the unprivileged `sandbox` user (UID 1000).

**Limits of this design.** One sandbox container serves every user of the backend and all commands run as the same
UID, so a malicious command can read other users' files on the shared volume or see other processes. For strict
multi-tenant isolation, run one sandbox (with its own volume) per organization, or use a hardened runtime such as
gVisor (`runtime: runsc`) or Kata Containers, and restrict outbound network access with a network policy. The token
only authorizes this API; rotate it if you suspect a leak.

## Development

```bash
cd sandbox
pip install -r requirements.txt
SANDBOX_INSECURE=1 SANDBOX_ROOT=/tmp/ren-workspaces uvicorn server:app --port 8080
```

The backend test-suite exercises this server in-process (`backend/tests/test_tools_compute_sandbox.py`, using
FastAPI's TestClient with `SANDBOX_INSECURE=1` and a temporary `SANDBOX_ROOT`).
