"""Ren execution sandbox: a tiny FastAPI service that runs commands on behalf of the Ren backend.

It is deployed as its own container (see Dockerfile and README.md). The backend and the sandbox mount the same volume
at /data, so the absolute workspace paths the backend sends are valid here; every working directory must resolve
inside SANDBOX_ROOT (default /data/workspaces). Requests must carry ``Authorization: Bearer $SANDBOX_TOKEN`` unless
SANDBOX_INSECURE=1 (local development only).

Endpoints
  GET    /health                                   liveness and capabilities (no auth)
  POST   /exec    {command, cwd, timeout, env, stdin, roots, path_prepend, max_output}
                  → {exit_code, stdout, stderr, duration_ms, truncated, timed_out}
  POST   /python  {code, cwd, timeout, env, roots, path_prepend, max_output}  (same response)
  POST   /jobs    {command, cwd, timeout, env, owner, cwd_label, roots, path_prepend}   start a background job
  GET    /jobs?owner=...                           list jobs
  GET    /jobs/{id}?owner=&max_chars=&all=         status + output produced since the previous poll
  DELETE /jobs/{id}?owner=                         kill a job (and its process group)

`command` is a shell command line (string → ``/bin/bash -c``) or an argv list (executed directly). Every process runs
in its own process group, is killed with the whole group on timeout, has stdin closed unless input is supplied, and
has its output capped (head and tail are kept). Optional rlimits are applied through ``prlimit`` when available.
"""
from __future__ import annotations

import codecs
import collections
import hmac
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

VERSION = "1.0.0"
IS_WINDOWS = os.name == "nt"
MAX_COMMAND_CHARS = 100_000
JOB_BUFFER_CHARS = 64_000
JOB_RETENTION_S = 3600

log = logging.getLogger("ren.sandbox")


# --------------------------------------------------------------------------- configuration (read per request)
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    token: str
    insecure: bool
    root: Path
    max_output: int
    max_timeout: int
    max_job_timeout: int
    max_jobs: int
    max_jobs_per_owner: int
    max_file_mb: int
    max_procs: int
    max_memory_mb: int


def config() -> Config:
    return Config(
        token=os.environ.get("SANDBOX_TOKEN", ""),
        insecure=_truthy(os.environ.get("SANDBOX_INSECURE")),
        root=Path(os.environ.get("SANDBOX_ROOT") or "/data/workspaces").resolve(),
        max_output=_env_int("SANDBOX_MAX_OUTPUT", 30_000),
        max_timeout=_env_int("SANDBOX_MAX_TIMEOUT", 600),
        max_job_timeout=_env_int("SANDBOX_MAX_JOB_TIMEOUT", 4 * 3600),
        max_jobs=_env_int("SANDBOX_MAX_JOBS", 32),
        max_jobs_per_owner=_env_int("SANDBOX_MAX_JOBS_PER_OWNER", 5),
        max_file_mb=_env_int("SANDBOX_MAX_FILE_MB", 1024),
        max_procs=_env_int("SANDBOX_MAX_PROCS", 0),
        max_memory_mb=_env_int("SANDBOX_MAX_MEMORY_MB", 0),
    )


def require_auth(authorization: str | None = Header(default=None)) -> None:
    cfg = config()
    if cfg.insecure:
        return
    if not cfg.token:
        raise HTTPException(503, "SANDBOX_TOKEN is not configured on the sandbox (SANDBOX_INSECURE=1 is for local "
                                 "development only).")
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), cfg.token.encode()):
        raise HTTPException(401, "Invalid or missing bearer token.", headers={"WWW-Authenticate": "Bearer"})


# --------------------------------------------------------------------------- request models
class _Base(BaseModel):
    cwd: str
    timeout: int = 120
    env: dict[str, str] = Field(default_factory=dict)
    roots: list[str] = Field(default_factory=list)
    path_prepend: list[str] = Field(default_factory=list)


class ExecRequest(_Base):
    command: str | list[str]
    stdin: str | None = None
    max_output: int | None = None


class PythonRequest(_Base):
    code: str
    max_output: int | None = None


class JobRequest(_Base):
    command: str | list[str]
    timeout: int = 1800
    owner: str = ""
    cwd_label: str | None = None


# --------------------------------------------------------------------------- validation helpers
def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _check_cwd(req: _Base, cfg: Config) -> Path:
    raw = req.cwd or ""
    if not raw or "\x00" in raw:
        raise HTTPException(400, "cwd is required.")
    p = Path(raw)
    if not p.is_absolute():
        raise HTTPException(400, "cwd must be an absolute path.")
    resolved = p.resolve()
    if not _inside(resolved, cfg.root):
        raise HTTPException(403, "cwd is outside the sandbox root.")
    if req.roots:
        roots = [Path(r).resolve() for r in req.roots if r and "\x00" not in r and Path(r).is_absolute()]
        roots = [r for r in roots if _inside(r, cfg.root)]
        if not any(_inside(resolved, r) for r in roots):
            raise HTTPException(403, "cwd is outside the allowed roots.")
    if not resolved.is_dir():
        raise HTTPException(404, "cwd does not exist.")
    return resolved


def _check_command(command: str | list[str]) -> None:
    parts = [command] if isinstance(command, str) else list(command)
    if not parts or not parts[0] or not all(isinstance(a, str) for a in parts):
        raise HTTPException(400, "command is empty or invalid.")
    if isinstance(command, str) and not command.strip():
        raise HTTPException(400, "command is empty.")
    if any("\x00" in a for a in parts):
        raise HTTPException(400, "command contains a NUL byte.")
    if sum(len(a) for a in parts) > MAX_COMMAND_CHARS:
        raise HTTPException(413, "command is too long.")


_BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ", "HOME", "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR", "TEMP", "TMP")
_BLOCKED_ENV = {"LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "BASH_ENV", "ENV", "PROMPT_COMMAND", "SHELLOPTS",
                "BASHOPTS", "PS4", "IFS", "SANDBOX_TOKEN"}
_COMMON_ENV = {
    "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "MPLBACKEND": "Agg", "TERM": "dumb", "NO_COLOR": "1",
    "PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1", "npm_config_update_notifier": "false", "npm_config_fund": "false",
}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _child_env(req: _Base, cfg: Config) -> dict[str, str]:
    """Minimal base environment (never the sandbox's own secrets) + the backend-supplied variables."""
    env = {k: os.environ[k] for k in _BASE_ENV_KEYS if k in os.environ}
    env.setdefault("PATH", os.defpath)
    if not IS_WINDOWS:
        env.setdefault("LANG", "C.UTF-8")
    env.update(_COMMON_ENV)
    for key, value in req.env.items():
        if _ENV_NAME.match(key) and key.upper() not in _BLOCKED_ENV:
            env[key] = str(value)
    prepend = []
    for raw in req.path_prepend:
        if raw and "\x00" not in raw and Path(raw).is_absolute() and _inside(Path(raw).resolve(), cfg.root):
            prepend.append(str(Path(raw).resolve()))
    if prepend:
        env["PATH"] = os.pathsep.join(prepend + [env["PATH"]])
    return env


def _windows_bash() -> str | None:  # development / tests on Windows only
    git = shutil.which("git")
    roots = list(Path(git).resolve().parents)[:3] if git else []
    roots += [Path(os.environ.get(v, "")) / "Git" for v in ("PROGRAMW6432", "PROGRAMFILES") if os.environ.get(v)]
    for root in roots:
        if (root / "bin" / "bash.exe").is_file():
            return str(root / "bin" / "bash.exe")
    return None


def _shell(command: str) -> list[str]:
    if IS_WINDOWS:
        bash = _windows_bash()
        return [bash, "-c", command] if bash else ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/bash", "-c", command] if os.path.exists("/bin/bash") else ["/bin/sh", "-c", command]


def _limits(cfg: Config) -> list[str]:
    """prlimit prefix applying per-process rlimits (container-level limits are still recommended)."""
    if IS_WINDOWS or not shutil.which("prlimit"):
        return []
    args = ["prlimit", "--core=0"]
    if cfg.max_file_mb > 0:
        args.append(f"--fsize={cfg.max_file_mb * 1024 * 1024}")
    if cfg.max_procs > 0:
        args.append(f"--nproc={cfg.max_procs}")
    if cfg.max_memory_mb > 0:
        args.append(f"--as={cfg.max_memory_mb * 1024 * 1024}")
    return [*args, "--"]


def _argv(command: str | list[str], cfg: Config) -> list[str]:
    return [*_limits(cfg), *(_shell(command) if isinstance(command, str) else list(command))]


# --------------------------------------------------------------------------- process runner
class RingBuffer:
    """Thread-safe buffer keeping the last `cap` characters, addressable by absolute offsets."""

    def __init__(self, cap: int):
        self.cap = max(1, cap)
        self._chunks: collections.deque[str] = collections.deque()
        self._len = 0
        self.total = 0
        self._lock = threading.Lock()

    def write(self, s: str) -> None:
        if not s:
            return
        with self._lock:
            self._chunks.append(s)
            self._len += len(s)
            self.total += len(s)
            while self._len > self.cap and self._chunks:
                over = self._len - self.cap
                first = self._chunks[0]
                if len(first) <= over:
                    self._chunks.popleft()
                    self._len -= len(first)
                else:
                    self._chunks[0] = first[over:]
                    self._len -= over

    def read(self, since: int = 0, max_chars: int | None = None) -> tuple[str, int, int]:
        with self._lock:
            start = self.total - self._len
            dropped = max(0, start - since)
            since = max(since, start)
            data = "".join(self._chunks)
        text = data[since - start:]
        if max_chars is not None:
            text = text[:max_chars]
        return text, since + len(text), dropped


class HeadTail:
    """Keeps the first ~40% and last ~60% of `limit` characters."""

    def __init__(self, limit: int):
        self.head_cap = max(1, limit * 2 // 5)
        self.head: list[str] = []
        self.head_len = 0
        self.tail = RingBuffer(max(1, limit - self.head_cap))

    def write(self, s: str) -> None:
        if self.head_len < self.head_cap:
            take = s[: self.head_cap - self.head_len]
            self.head.append(take)
            self.head_len += len(take)
            s = s[len(take):]
        if s:
            self.tail.write(s)

    def text(self) -> tuple[str, bool]:
        tail, _, dropped = self.tail.read(0)
        head = "".join(self.head)
        return (f"{head}\n…[{dropped} chars omitted]…\n{tail}", True) if dropped else (head + tail, False)


def _pump(pipe: Any, sink: Any) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        read = getattr(pipe, "read1", pipe.read)
        while chunk := read(65536):
            sink.write(decoder.decode(chunk))
        sink.write(decoder.decode(b"", final=True))
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _feed(pipe: Any, text: str) -> None:
    try:
        pipe.write(text.encode("utf-8"))
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _thread(fn: Any, *args: Any) -> threading.Thread:
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t


def _spawn(argv: list[str], cwd: Path, env: dict[str, str], with_stdin: bool) -> subprocess.Popen:
    kwargs: dict[str, Any] = {"cwd": str(cwd), "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                              "stdin": subprocess.PIPE if with_stdin else subprocess.DEVNULL}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def _kill_tree(proc: subprocess.Popen) -> None:
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            log.debug("taskkill failed for pid %s", proc.pid)
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


def run(argv: list[str], cwd: Path, env: dict[str, str], timeout: int, stdin: str | None, max_output: int) -> dict:
    t0 = time.monotonic()
    try:
        proc = _spawn(argv, cwd, env, stdin is not None)
    except FileNotFoundError:
        return {"exit_code": 127, "stdout": "", "stderr": f"Command not found: {argv[0]}", "duration_ms": 0,
                "truncated": False, "timed_out": False}
    except OSError as e:
        return {"exit_code": 126, "stdout": "", "stderr": f"Could not start {argv[0]}: {e}", "duration_ms": 0,
                "truncated": False, "timed_out": False}
    out, err = HeadTail(max_output), HeadTail(max_output)
    readers = [_thread(_pump, proc.stdout, out), _thread(_pump, proc.stderr, err)]
    if stdin is not None:
        _thread(_feed, proc.stdin, stdin)
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    for t in readers:
        t.join(timeout=2.0)
    if any(t.is_alive() for t in readers):  # background children keep the pipes open
        _kill_tree(proc)
        for t in readers:
            t.join(timeout=2.0)
    stdout, t1 = out.text()
    stderr, t2 = err.text()
    if timed_out:
        stderr = (stderr.rstrip("\n") + f"\n[killed after the {timeout}s timeout]").lstrip("\n")
    return {"exit_code": proc.returncode if proc.returncode is not None else -9, "stdout": stdout, "stderr": stderr,
            "duration_ms": int((time.monotonic() - t0) * 1000), "truncated": t1 or t2, "timed_out": timed_out}


# --------------------------------------------------------------------------- background jobs
@dataclass
class Job:
    id: str
    owner: str
    command: str
    cwd: str
    timeout: int
    started_at: float
    t0: float
    proc: subprocess.Popen | None = None
    buffer: RingBuffer = field(default_factory=lambda: RingBuffer(JOB_BUFFER_CHARS))
    status: str = "running"  # running | exited | killed | timeout
    exit_code: int | None = None
    ended_at: float | None = None
    cursor: int = 0
    kill_requested: bool = False

    def summary(self) -> dict:
        end = self.ended_at or time.time()
        command = self.command if len(self.command) <= 300 else self.command[:300] + "…"
        return {"job_id": self.id, "status": self.status, "exit_code": self.exit_code, "command": command,
                "cwd": self.cwd, "started_at": _iso(self.started_at), "ended_at": _iso(self.ended_at),
                "runtime_s": round(end - self.started_at, 1), "pid": self.proc.pid if self.proc else None}


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _monitor(job: Job, readers: list[threading.Thread]) -> None:
    proc = job.proc
    assert proc is not None
    timed_out = False
    while True:
        try:
            proc.wait(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if job.kill_requested:
                break
            if time.monotonic() - job.t0 >= job.timeout:
                timed_out = True
                break
    if job.kill_requested or timed_out:
        _kill_tree(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    for t in readers:
        t.join(timeout=2.0)
    if any(t.is_alive() for t in readers):
        _kill_tree(proc)
        for t in readers:
            t.join(timeout=2.0)
    if timed_out:
        job.buffer.write(f"\n[killed after the {job.timeout}s timeout]\n")
    job.exit_code = proc.returncode
    job.ended_at = time.time()
    job.status = "killed" if job.kill_requested else ("timeout" if timed_out else "exited")


def _prune() -> None:
    now = time.time()
    for job_id, job in list(_jobs.items()):
        if job.status != "running" and job.ended_at and now - job.ended_at > JOB_RETENTION_S:
            _jobs.pop(job_id, None)
    finished = sorted((j for j in _jobs.values() if j.status != "running"), key=lambda j: j.started_at)
    while len(_jobs) > 500 and finished:
        _jobs.pop(finished.pop(0).id, None)


def _get_job(job_id: str, owner: str) -> Job:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None or job.owner != owner:
        raise HTTPException(404, f"No background job '{job_id}'.")
    return job


def _kill_all() -> None:
    with _jobs_lock:
        running = [j for j in _jobs.values() if j.status == "running" and j.proc is not None]
    for job in running:
        job.kill_requested = True
        _kill_tree(job.proc)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        import anyio.to_thread

        anyio.to_thread.current_default_thread_limiter().total_tokens = _env_int("SANDBOX_THREADS", 64)
    except Exception:  # noqa: BLE001
        pass
    cfg = config()
    if not cfg.insecure and not cfg.token:
        log.error("SANDBOX_TOKEN is not set: all execution requests will be refused.")
    yield
    _kill_all()


app = FastAPI(title="Ren sandbox", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    cfg = config()
    with _jobs_lock:
        running = sum(1 for j in _jobs.values() if j.status == "running")
    return {"ok": True, "version": VERSION,
            "auth": "insecure" if cfg.insecure else ("token" if cfg.token else "unconfigured"),
            "root_ready": cfg.root.is_dir(), "python": sys.version.split()[0],
            "tools": {name: shutil.which(name) is not None for name in ("bash", "git", "node", "npm", "prlimit")},
            "running_jobs": running}


@app.post("/exec", dependencies=[Depends(require_auth)])
def exec_command(req: ExecRequest) -> dict:
    cfg = config()
    _check_command(req.command)
    cwd = _check_cwd(req, cfg)
    timeout = max(1, min(int(req.timeout), cfg.max_timeout))
    max_output = max(1000, min(int(req.max_output or cfg.max_output), cfg.max_output))
    return run(_argv(req.command, cfg), cwd, _child_env(req, cfg), timeout, req.stdin, max_output)


@app.post("/python", dependencies=[Depends(require_auth)])
def exec_python(req: PythonRequest) -> dict:
    cfg = config()
    if not req.code.strip():
        raise HTTPException(400, "code is empty.")
    if "\x00" in req.code:
        raise HTTPException(400, "code contains a NUL byte.")
    cwd = _check_cwd(req, cfg)
    timeout = max(1, min(int(req.timeout), cfg.max_timeout))
    max_output = max(1000, min(int(req.max_output or cfg.max_output), cfg.max_output))
    argv = [*_limits(cfg), sys.executable or "python3", "-u", "-"]  # code via stdin: the cwd stays importable
    return run(argv, cwd, _child_env(req, cfg), timeout, req.code, max_output)


@app.post("/jobs", dependencies=[Depends(require_auth)])
def start_job(req: JobRequest) -> dict:
    cfg = config()
    _check_command(req.command)
    cwd = _check_cwd(req, cfg)
    timeout = max(1, min(int(req.timeout), cfg.max_job_timeout))
    command_text = req.command if isinstance(req.command, str) else " ".join(req.command)
    with _jobs_lock:
        _prune()
        running = [j for j in _jobs.values() if j.status == "running"]
        if len(running) >= cfg.max_jobs:
            raise HTTPException(429, f"The sandbox is running its maximum of {cfg.max_jobs} background jobs.")
        if sum(1 for j in running if j.owner == req.owner) >= cfg.max_jobs_per_owner:
            raise HTTPException(429, f"You already have {cfg.max_jobs_per_owner} background jobs running; stop one "
                                     "first.")
        job = Job(id=uuid.uuid4().hex[:12], owner=req.owner, command=command_text, cwd=req.cwd_label or str(cwd),
                  timeout=timeout, started_at=time.time(), t0=time.monotonic())
        _jobs[job.id] = job
    try:
        job.proc = _spawn(_argv(req.command, cfg), cwd, _child_env(req, cfg), False)
    except OSError as e:
        with _jobs_lock:
            _jobs.pop(job.id, None)
        raise HTTPException(400, f"Could not start the command: {e}") from e
    readers = [_thread(_pump, job.proc.stdout, job.buffer), _thread(_pump, job.proc.stderr, job.buffer)]
    _thread(_monitor, job, readers)
    return job.summary()


@app.get("/jobs", dependencies=[Depends(require_auth)])
def list_jobs(owner: str = Query("")) -> dict:
    with _jobs_lock:
        _prune()
        jobs = sorted((j for j in _jobs.values() if j.owner == owner), key=lambda j: j.started_at, reverse=True)
    return {"jobs": [j.summary() for j in jobs]}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_auth)])
def poll_job(job_id: str, owner: str = Query(""), max_chars: int = Query(10_000, ge=1, le=100_000),
             all: bool = Query(False)) -> dict:  # noqa: A002
    job = _get_job(job_id, owner)
    text, nxt, dropped = job.buffer.read(0 if all else job.cursor, max_chars)
    job.cursor = max(job.cursor, nxt)
    return {**job.summary(), "output": text, "dropped_chars": dropped, "more_output": job.buffer.total > nxt}


@app.delete("/jobs/{job_id}", dependencies=[Depends(require_auth)])
def kill_job(job_id: str, owner: str = Query("")) -> dict:
    job = _get_job(job_id, owner)
    if job.status == "running":
        job.kill_requested = True
        deadline = time.monotonic() + 15
        while job.status == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
    tail, _, _ = job.buffer.read(max(0, job.buffer.total - 2000))
    return {**job.summary(), "output_tail": tail}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host=os.environ.get("SANDBOX_HOST", "0.0.0.0"), port=_env_int("PORT", 8080))
