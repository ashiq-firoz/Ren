"""Shared command execution for the shell / code / git / container tool groups.

Where commands run
  * ``settings.sandbox_url`` set → the remote execution sandbox (``sandbox/server.py``, Docker deployments). Backend and
    sandbox mount the same volume at ``/data``, so the absolute workspace paths computed by ``Workspace.resolve()`` are
    valid in both containers; requests carry ``Authorization: Bearer <settings.sandbox_token>``.
  * otherwise → locally on the backend host, in a worker thread, with the user's workspace folder as cwd and a minimal
    environment derived from ``Workspace.env()`` (no backend secrets).

Every command runs with stdin closed (unless input is supplied), a wall-clock timeout that kills the whole process
tree, and bounded output capture (head + tail are kept). Background jobs are tracked per org + user and keep a ring
buffer of their most recent output.

Public helpers used by the tool modules:
  run_command, run_python, start_job, poll_job, kill_job, list_jobs, which, env_names,
  ws_path, ws_dir, require_write, exec_risk, sandboxed, format_result, clip, fence, python_exe.
"""
from __future__ import annotations

import asyncio
import atexit
import codecs
import collections
import functools
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
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.services.workspace import WorkspaceError
from app.tools.base import Risk, ToolContext, ToolError

log = logging.getLogger("ren.tools.exec")

IS_WINDOWS = os.name == "nt"

MAX_OUTPUT_CHARS = 30_000  # per stream, per command
MAX_TIMEOUT = 600  # foreground commands (seconds)
DEFAULT_JOB_TIMEOUT = 1800
MAX_JOB_TIMEOUT = 4 * 3600  # background jobs
JOB_BUFFER_CHARS = 64_000  # ring buffer of recent background-job output
MAX_RUNNING_JOBS_PER_USER = 5
MAX_KEPT_JOBS_PER_USER = 20
TOOL_TIMEOUT = MAX_TIMEOUT + 30  # executor-level timeout for tools that run commands
MAX_COMMAND_CHARS = 100_000

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROGRAM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

# Non-secret Windows variables many CLIs need (Workspace.env() keeps only the portable minimum).
_WIN_PASSTHROUGH = (
    "WINDIR", "SYSTEMDRIVE", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "APPDATA", "LOCALAPPDATA", "USERNAME",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS",
)
# Make tools non-interactive and their output plain.
_COMMON_ENV = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",
    "MPLBACKEND": "Agg",
    "TERM": "dumb",
    "NO_COLOR": "1",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
    "npm_config_update_notifier": "false",
    "npm_config_fund": "false",
    "DEBIAN_FRONTEND": "noninteractive",
}

_EXEC_POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("REN_EXEC_THREADS", "32")),
                                thread_name_prefix="ren-exec")


# --------------------------------------------------------------------------- mode / risk
def sandboxed() -> bool:
    """True when commands are sent to the remote execution sandbox."""
    return bool((settings.sandbox_url or "").strip())


def exec_mode() -> str:
    return "sandbox" if sandboxed() else "local"


def exec_risk(_args: dict | None = None) -> Risk:
    """Risk of running arbitrary code: MEDIUM inside the remote sandbox, HIGH on the backend host."""
    return Risk.MEDIUM if sandboxed() else Risk.HIGH


def where_label() -> str:
    return "in the isolated sandbox" if sandboxed() else "on the Ren server host (not sandboxed)"


def python_exe() -> str:
    """Python interpreter for code tools: the sandbox's `python`, or the backend's own interpreter locally."""
    return "python" if sandboxed() else sys.executable


# --------------------------------------------------------------------------- workspace helpers
def ws_path(ctx: ToolContext, path: str | None, *, must_exist: bool = False, for_write: bool = False) -> Path:
    """Resolve a virtual workspace path, converting sandbox violations into ToolError."""
    try:
        return ctx.workspace.resolve(path, must_exist=must_exist, for_write=for_write)
    except WorkspaceError as e:
        raise ToolError(str(e)) from e


def ws_dir(ctx: ToolContext, path: str | None) -> Path:
    p = ws_path(ctx, path, must_exist=True)
    if not p.is_dir():
        raise ToolError(f"Not a folder: {ctx.workspace.normalize(path)}")
    return p


def require_write(ctx: ToolContext, what: str = "modify files") -> None:
    if not ctx.can_write:
        raise ToolError(f"Your role ({ctx.role}) is read-only, so you cannot {what}.")


def user_pylib(ctx: ToolContext) -> Path:
    """Per-user Python package directory used by shell_install_package (pip --target)."""
    return ctx.workspace.user_root / ".ren" / "python"


def _roots(ctx: ToolContext) -> list[str]:
    return [str(ctx.workspace.user_root), str(ctx.workspace.shared_root)]


def _owner(ctx: ToolContext) -> str:
    return f"{ctx.org_id}:{ctx.workspace.user_id}"


# --------------------------------------------------------------------------- text helpers
def clip(text: str, limit: int, *, tail_ratio: float = 0.6) -> str:
    """Shorten `text` to about `limit` characters, keeping its head and tail."""
    if limit <= 0 or len(text) <= limit:
        return text
    tail = int(limit * tail_ratio)
    head = limit - tail
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…[{omitted} chars omitted]…\n{text[len(text) - tail:]}"


def fence(text: str, lang: str = "") -> str:
    """Markdown code fence that survives backticks inside `text`."""
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def format_result(res: dict, *, budget: int = 12_000) -> str:
    """Render a command result compactly for the model: status line, stdout, then stderr."""
    out = (res.get("stdout") or "").replace("\r\n", "\n")
    err = (res.get("stderr") or "").replace("\r\n", "\n")
    secs = (res.get("duration_ms") or 0) / 1000
    head = f"exit code {res.get('exit_code')} · {secs:.1f}s"
    if res.get("timed_out"):
        head += " · timed out (process killed)"
    if res.get("truncated"):
        head += " · output truncated"
    if len(out) + len(err) > budget:
        err_budget = min(len(err), max(budget // 3, budget - len(out)))
        out, err = clip(out, budget - err_budget), clip(err, err_budget)
    parts = [head]
    if out.strip():
        parts.append(out.rstrip("\n"))
    if err.strip():
        parts.append("[stderr]\n" + err.rstrip("\n"))
    if not out.strip() and not err.strip():
        parts.append("(no output)")
    return "\n".join(parts)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- environment
def build_env(ctx: ToolContext, extra: dict | None = None, *, remote: bool, user_paths: bool = True,
              passthrough: Iterable[str] = ()) -> tuple[dict[str, str], list[str]]:
    """Environment for a child process and the directories to prepend to PATH.

    Local: ``Workspace.env()`` (minimal, no backend secrets) + a few non-secret OS variables. Remote: only Ren's own
    variables — the sandbox supplies its base environment. `user_paths` adds the user's pip --target library to
    PYTHONPATH/PATH (only for tools that run user code anyway; internal tools must not pick up user-planted modules).
    """
    ws = ctx.workspace
    if remote:
        env = {"REN_WORKSPACE": str(ws.user_root), "REN_SHARED": str(ws.shared_root)}
        home = ws.user_root / ".ren" / "home"  # per-user HOME so dotfiles/caches don't leak between tenants
        home.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
    else:
        env = ws.env()
        if IS_WINDOWS:
            env.update({k: os.environ[k] for k in _WIN_PASSTHROUGH if k in os.environ})
        env.update({k: os.environ[k] for k in passthrough if k in os.environ})
    env.update(_COMMON_ENV)
    env["REN_OUTPUT"] = str(ws.user_root / "output")
    tmp = ws.user_root / "temp"
    tmp.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(tmp)
    prepend: list[str] = []
    if user_paths:
        pylib = user_pylib(ctx)
        if pylib.is_dir():
            env["PYTHONPATH"] = str(pylib)
            if (pylib / "bin").is_dir():
                prepend.append(str(pylib / "bin"))
    for k, v in (extra or {}).items():
        if not _ENV_NAME.match(str(k)):
            raise ToolError(f"Invalid environment variable name: {k!r}")
        env[str(k)] = str(v)
    if prepend and not remote:
        env["PATH"] = os.pathsep.join(prepend + [env.get("PATH", "")])
    return env, prepend


# --------------------------------------------------------------------------- local program resolution
def find_program(name: str, path: str | None = None) -> str | None:
    """Locate an executable on PATH the way the OS would run it.

    On Windows only files with a PATHEXT extension count (shutil.which may return extensionless stubs, e.g. a
    zero-byte ``System32\\docker``, which CreateProcess can't execute).
    """
    if not IS_WINDOWS:
        return shutil.which(name, path=path)
    if os.path.dirname(name):
        return name if os.path.isfile(name) else None
    exts = [e for e in (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
    has_ext = os.path.splitext(name)[1].upper() in {e.upper() for e in exts}
    candidates = [name] if has_ext else [name + e.lower() for e in exts]
    for folder in (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep):
        folder = folder.strip().strip('"')
        if not folder:
            continue
        for candidate in candidates:
            full = os.path.join(folder, candidate)
            if os.path.isfile(full):
                return full
    return None


def _is_wsl_launcher(path: str) -> bool:
    p = path.lower().replace("/", "\\")
    sysroot = os.environ.get("SYSTEMROOT", r"C:\Windows").lower()
    return p.startswith(sysroot + "\\system32") or "\\windowsapps\\" in p


@functools.lru_cache(maxsize=1)
def _windows_bash() -> tuple[str, bool] | None:
    """(bash.exe, needs_login_shell) for Git Bash on Windows, never the WSL launcher; None if unavailable."""
    roots: list[Path] = []
    git = find_program("git")
    if git:
        roots.extend(list(Path(git).resolve().parents)[:3])
    for var in ("PROGRAMW6432", "PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            roots += [Path(base) / "Git", Path(base) / "Programs" / "Git"]
    for root in roots:
        candidate = root / "bin" / "bash.exe"  # Git's launcher sets up PATH (/usr/bin, /mingw64/bin) itself
        if candidate.is_file():
            return str(candidate), False
    found = find_program("bash")
    if found and not _is_wsl_launcher(found):
        return found, True  # raw MSYS/Cygwin bash: a login shell puts /usr/bin on PATH
    return None


def shell_argv(command: str) -> list[str]:
    """argv that runs a shell command line on this host."""
    if IS_WINDOWS:
        bash = _windows_bash()
        if bash:
            exe, login = bash
            return [exe, "-lc" if login else "-c", command]
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command]
    if os.path.exists("/bin/bash"):
        return ["/bin/bash", "-lc", command]
    return ["/bin/sh", "-c", command]


def shell_label() -> str:
    if sandboxed():
        return "bash (sandbox)"
    if IS_WINDOWS:
        return "bash (Git Bash)" if _windows_bash() else "Windows PowerShell"
    return "bash" if os.path.exists("/bin/bash") else "sh"


def _resolve_local_argv(argv: list[str], env: dict[str, str]) -> list[str]:
    """Windows: resolve PATHEXT shims (npm.cmd…) safely; POSIX: Popen already searches env['PATH']."""
    if not IS_WINDOWS:
        return argv
    exe = argv[0]
    if exe in ("npm", "npx"):
        node = find_program("node", env.get("PATH"))
        if node:  # run npm's JS entry point directly: no cmd.exe, no argument re-parsing
            cli = Path(node).parent / "node_modules" / "npm" / "bin" / f"{exe}-cli.js"
            if cli.is_file():
                return [node, str(cli), *argv[1:]]
    found = find_program(exe, env.get("PATH"))
    if not found:
        return argv
    if found.lower().endswith((".cmd", ".bat")):
        unsafe = set('&|<>^%"\r\n')
        for arg in argv[1:]:
            if any(c in unsafe for c in arg):
                raise ToolError(f"Argument {arg!r} contains characters that are unsafe to pass to "
                                f"{Path(found).name} on Windows.")
    return [found, *argv[1:]]


# --------------------------------------------------------------------------- output capture
class RingBuffer:
    """Thread-safe text buffer keeping the last `cap` characters, with absolute offsets."""

    def __init__(self, cap: int = JOB_BUFFER_CHARS):
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
        """Text written after absolute offset `since` → (text, next_offset, dropped_chars)."""
        with self._lock:
            start = self.total - self._len
            dropped = max(0, start - since)
            since = max(since, start)
            data = "".join(self._chunks)
        text = data[since - start:]
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars]
        return text, since + len(text), dropped


class _HeadTail:
    """Keeps the first ~40% and the last ~60% of `limit` characters of a stream."""

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
        head = "".join(self.head)
        tail, _, dropped = self.tail.read(0)
        if dropped:
            return f"{head}\n…[{dropped} chars omitted]…\n{tail}", True
        return head + tail, False


def _pump(pipe: Any, sink: Any) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        read = getattr(pipe, "read1", pipe.read)
        while True:
            chunk = read(65536)
            if not chunk:
                break
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
    t = threading.Thread(target=fn, args=args, daemon=True, name=f"ren-exec-{fn.__name__}")
    t.start()
    return t


# --------------------------------------------------------------------------- process control
def _spawn(argv: Sequence[str], cwd: Path, env: dict[str, str], *, with_stdin: bool) -> subprocess.Popen:
    kwargs: dict[str, Any] = {
        "cwd": str(cwd), "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        "stdin": subprocess.PIPE if with_stdin else subprocess.DEVNULL,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True  # own process group → the whole tree can be killed
    return subprocess.Popen(list(argv), **kwargs)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a process and all of its children."""
    if IS_WINDOWS:
        taskkill = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe")
        try:
            subprocess.run([taskkill, "/F", "/T", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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


def _result(exit_code: int, stdout: str, stderr: str, duration_ms: int, *, truncated: bool = False,
            timed_out: bool = False) -> dict:
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr, "duration_ms": duration_ms,
            "truncated": truncated, "timed_out": timed_out}


def _run_local_sync(argv: list[str], cwd: Path, env: dict[str, str], timeout: int, stdin: str | None,
                    max_output: int, cancel: threading.Event) -> dict:
    t0 = time.monotonic()
    try:
        proc = _spawn(argv, cwd, env, with_stdin=stdin is not None)
    except FileNotFoundError:
        return _result(127, "", f"Command not found: {argv[0]}", 0)
    except OSError as e:
        return _result(126, "", f"Could not start {argv[0]}: {e}", 0)
    out, err = _HeadTail(max_output), _HeadTail(max_output)
    readers = [_thread(_pump, proc.stdout, out), _thread(_pump, proc.stderr, err)]
    if stdin is not None:
        _thread(_feed, proc.stdin, stdin)
    deadline = t0 + timeout
    timed_out = cancelled = False
    while True:
        try:
            proc.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if cancel.is_set():
                cancelled = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
    if timed_out or cancelled:
        _kill_tree(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    for t in readers:
        t.join(timeout=2.0)
    if any(t.is_alive() for t in readers):  # leftover background children still hold the output pipes
        _kill_tree(proc)
        for t in readers:
            t.join(timeout=2.0)
    stdout, t1 = out.text()
    stderr, t2 = err.text()
    if timed_out:
        stderr = (stderr.rstrip("\n") + f"\n[killed after the {timeout}s timeout]").lstrip("\n")
    elif cancelled:
        stderr = (stderr.rstrip("\n") + "\n[killed: cancelled]").lstrip("\n")
    rc = proc.returncode if proc.returncode is not None else -9
    return _result(rc, stdout, stderr, int((time.monotonic() - t0) * 1000), truncated=t1 or t2, timed_out=timed_out)


async def _run_local(argv: list[str], cwd: Path, env: dict[str, str], timeout: int, stdin: str | None,
                     max_output: int) -> dict:
    cancel = threading.Event()
    fut = asyncio.get_running_loop().run_in_executor(
        _EXEC_POOL, _run_local_sync, argv, cwd, env, timeout, stdin, max_output, cancel)
    try:
        return await fut
    except asyncio.CancelledError:
        cancel.set()  # the worker thread kills the process tree
        raise


# --------------------------------------------------------------------------- remote sandbox client
_sandbox_transport: httpx.AsyncBaseTransport | None = None  # tests inject an ASGI transport here


async def sandbox_request(method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                          timeout: float = 60.0) -> Any:
    base = (settings.sandbox_url or "").rstrip("/")
    headers = {"Authorization": f"Bearer {settings.sandbox_token}"} if settings.sandbox_token else {}
    try:
        async with httpx.AsyncClient(transport=_sandbox_transport, timeout=httpx.Timeout(timeout, connect=10.0),
                                     follow_redirects=False) as client:
            resp = await client.request(method, f"{base}{path}", json=json, params=params, headers=headers)
    except httpx.TimeoutException as e:
        raise ToolError(f"The execution sandbox did not answer within {int(timeout)}s.") from e
    except httpx.HTTPError as e:
        raise ToolError(f"The execution sandbox is unreachable ({type(e).__name__}). Check SANDBOX_URL and that "
                        "the sandbox service is running.") from e
    if resp.status_code == 401:
        raise ToolError("The execution sandbox rejected the backend's credentials (check SANDBOX_TOKEN on both "
                        "services).")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except Exception:  # noqa: BLE001
            detail = resp.text[:300]
        raise ToolError(f"Sandbox error ({resp.status_code}): {detail}")
    try:
        return resp.json()
    except ValueError as e:
        raise ToolError("The execution sandbox returned an invalid response (is SANDBOX_URL pointing at it?).") from e


def _normalize(res: dict) -> dict:
    return _result(int(res.get("exit_code", -1)), str(res.get("stdout") or ""), str(res.get("stderr") or ""),
                   int(res.get("duration_ms") or 0), truncated=bool(res.get("truncated")),
                   timed_out=bool(res.get("timed_out")))


# --------------------------------------------------------------------------- run_command / run_python
def _check_command(command: str | Sequence[str]) -> None:
    if isinstance(command, str):
        if not command.strip():
            raise ToolError("The command is empty.")
        parts = [command]
    else:
        parts = list(command)
        if not parts or not all(isinstance(a, str) for a in parts) or not parts[0]:
            raise ToolError("Invalid command.")
    if any("\x00" in a for a in parts):
        raise ToolError("The command contains a NUL byte.")
    if sum(len(a) for a in parts) > MAX_COMMAND_CHARS:
        raise ToolError(f"The command is too long (max {MAX_COMMAND_CHARS:,} characters).")


def clamp_timeout(timeout: Any, maximum: int) -> int:
    try:
        t = int(timeout)
    except (TypeError, ValueError):
        t = settings.shell_timeout_seconds
    return max(1, min(t, maximum))


def _redact(res: dict, secrets: Iterable[str]) -> dict:
    for s in (s for s in secrets if s and len(s) >= 4):
        res["stdout"] = res["stdout"].replace(s, "***")
        res["stderr"] = res["stderr"].replace(s, "***")
    return res


async def run_command(ctx: ToolContext, command: str | list[str], *, cwd_virtual: str = "workspace", timeout: int,
                      env: dict | None = None, stdin: str | None = None, max_output: int = MAX_OUTPUT_CHARS,
                      user_paths: bool = True, local: bool = False, passthrough: Iterable[str] = (),
                      redact: Iterable[str] = ()) -> dict:
    """Run a command in the user's workspace.

    `command` is either a shell command line (str → bash / PowerShell) or an argv list (executed directly, no shell).
    Returns ``{exit_code, stdout, stderr, duration_ms, truncated, timed_out}``. Runs in the remote sandbox when one is
    configured, unless `local=True` (host-infrastructure CLIs such as docker/kubectl). `passthrough` names extra host
    environment variables to forward (local only); `redact` lists secrets to scrub from the output.
    """
    _check_command(command)
    timeout = clamp_timeout(timeout, MAX_TIMEOUT)
    max_output = max(1000, min(int(max_output), MAX_OUTPUT_CHARS))
    cwd = ws_dir(ctx, cwd_virtual)
    remote = sandboxed() and not local
    child_env, prepend = build_env(ctx, env, remote=remote, user_paths=user_paths, passthrough=passthrough)
    if remote:
        payload = {"command": command, "cwd": str(cwd), "timeout": timeout, "env": child_env, "stdin": stdin,
                   "roots": _roots(ctx), "path_prepend": prepend, "max_output": max_output}
        res = _normalize(await sandbox_request("POST", "/exec", json=payload, timeout=timeout + 30))
    else:
        argv = shell_argv(command) if isinstance(command, str) else _resolve_local_argv(list(command), child_env)
        res = await _run_local(argv, cwd, child_env, timeout, stdin, max_output)
    return _redact(res, redact)


async def run_python(ctx: ToolContext, code: str, *, cwd_virtual: str = "workspace", timeout: int,
                     env: dict | None = None, max_output: int = MAX_OUTPUT_CHARS) -> dict:
    """Run a Python snippet (fed through stdin, so the cwd is importable) and return the command result."""
    if not code.strip():
        raise ToolError("The code is empty.")
    if "\x00" in code:
        raise ToolError("The code contains a NUL byte.")
    if sandboxed():
        t = clamp_timeout(timeout, MAX_TIMEOUT)
        cwd = ws_dir(ctx, cwd_virtual)
        child_env, prepend = build_env(ctx, env, remote=True)
        payload = {"code": code, "cwd": str(cwd), "timeout": t, "env": child_env, "roots": _roots(ctx),
                   "path_prepend": prepend, "max_output": max_output}
        return _normalize(await sandbox_request("POST", "/python", json=payload, timeout=t + 30))
    return await run_command(ctx, [sys.executable, "-u", "-"], cwd_virtual=cwd_virtual, timeout=timeout, env=env,
                             stdin=code, max_output=max_output)


# --------------------------------------------------------------------------- background jobs (local registry)
@dataclass
class _Job:
    id: str
    org_id: str
    user_id: str
    command: str
    cwd: str
    timeout: int
    started_at: float
    t0: float
    proc: subprocess.Popen | None = None
    buffer: RingBuffer = field(default_factory=RingBuffer)
    status: str = "running"  # running | exited | killed | timeout
    exit_code: int | None = None
    ended_at: float | None = None
    cursor: int = 0
    kill_requested: bool = False

    def summary(self) -> dict:
        end = self.ended_at or time.time()
        return {"job_id": self.id, "status": self.status, "exit_code": self.exit_code,
                "command": clip(self.command, 300), "cwd": self.cwd, "started_at": _iso(self.started_at),
                "ended_at": _iso(self.ended_at), "runtime_s": round(end - self.started_at, 1),
                "pid": self.proc.pid if self.proc else None}


_jobs: dict[str, _Job] = {}
_jobs_lock = threading.Lock()


def _monitor(job: _Job, readers: list[threading.Thread]) -> None:
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


def _prune_jobs(org_id: str, user_id: str) -> None:
    mine = sorted((j for j in _jobs.values() if j.org_id == org_id and j.user_id == user_id),
                  key=lambda j: j.started_at)
    finished = [j for j in mine if j.status != "running"]
    while len(mine) > MAX_KEPT_JOBS_PER_USER and finished:
        old = finished.pop(0)
        mine.remove(old)
        _jobs.pop(old.id, None)


def _get_job(ctx: ToolContext, job_id: str) -> _Job:
    with _jobs_lock:
        job = _jobs.get((job_id or "").strip())
    if job is None or job.org_id != ctx.org_id or job.user_id != ctx.workspace.user_id:
        raise ToolError(f"No background job '{job_id}' found for you. Use shell_processes to list your jobs.")
    return job


async def start_job(ctx: ToolContext, command: str, *, cwd_virtual: str = "workspace",
                    timeout: int = DEFAULT_JOB_TIMEOUT, env: dict | None = None) -> dict:
    """Start a shell command in the background; returns the job summary (poll it with poll_job)."""
    _check_command(command)
    timeout = clamp_timeout(timeout, MAX_JOB_TIMEOUT)
    cwd = ws_dir(ctx, cwd_virtual)
    vcwd = ctx.workspace.virtual(cwd) or "/"
    if sandboxed():
        child_env, prepend = build_env(ctx, env, remote=True)
        payload = {"command": command, "cwd": str(cwd), "cwd_label": vcwd, "timeout": timeout, "env": child_env,
                   "owner": _owner(ctx), "roots": _roots(ctx), "path_prepend": prepend}
        return await sandbox_request("POST", "/jobs", json=payload, timeout=30)
    child_env, _ = build_env(ctx, env, remote=False)
    argv = shell_argv(command)
    uid = ctx.workspace.user_id
    with _jobs_lock:
        _prune_jobs(ctx.org_id, uid)
        running = sum(1 for j in _jobs.values() if j.org_id == ctx.org_id and j.user_id == uid
                      and j.status == "running")
        if running >= MAX_RUNNING_JOBS_PER_USER:
            raise ToolError(f"You already have {running} background jobs running (max {MAX_RUNNING_JOBS_PER_USER}). "
                            "Stop one with shell_kill first.")
        job = _Job(id=uuid.uuid4().hex[:12], org_id=ctx.org_id, user_id=uid, command=command, cwd=vcwd,
                   timeout=timeout, started_at=time.time(), t0=time.monotonic())
        _jobs[job.id] = job
    try:
        job.proc = await asyncio.to_thread(_spawn, argv, cwd, child_env, with_stdin=False)
    except OSError as e:
        with _jobs_lock:
            _jobs.pop(job.id, None)
        raise ToolError(f"Could not start the command: {e}") from e
    readers = [_thread(_pump, job.proc.stdout, job.buffer), _thread(_pump, job.proc.stderr, job.buffer)]
    _thread(_monitor, job, readers)
    return job.summary()


async def poll_job(ctx: ToolContext, job_id: str, *, max_chars: int = 10_000, all_output: bool = False) -> dict:
    """Status of a background job plus its output since the previous poll (or all retained output)."""
    max_chars = max(200, min(int(max_chars), MAX_OUTPUT_CHARS))
    if sandboxed():
        params = {"owner": _owner(ctx), "max_chars": max_chars, "all": "true" if all_output else "false"}
        return await sandbox_request("GET", f"/jobs/{job_id}", params=params, timeout=30)
    job = _get_job(ctx, job_id)
    text, nxt, dropped = job.buffer.read(0 if all_output else job.cursor, max_chars)
    job.cursor = max(job.cursor, nxt)
    return {**job.summary(), "output": text, "dropped_chars": dropped, "more_output": job.buffer.total > nxt}


def _wait_job_end(job: _Job, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while job.status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)


async def kill_job(ctx: ToolContext, job_id: str) -> dict:
    """Stop a background job (and its child processes); returns its final summary and last output."""
    if sandboxed():
        return await sandbox_request("DELETE", f"/jobs/{job_id}", params={"owner": _owner(ctx)}, timeout=30)
    job = _get_job(ctx, job_id)
    if job.status == "running":
        job.kill_requested = True
        await asyncio.get_running_loop().run_in_executor(_EXEC_POOL, _wait_job_end, job, 15.0)
    tail, _, _ = job.buffer.read(max(0, job.buffer.total - 2000))
    return {**job.summary(), "output_tail": tail}


async def list_jobs(ctx: ToolContext) -> list[dict]:
    """This user's background jobs, newest first."""
    if sandboxed():
        res = await sandbox_request("GET", "/jobs", params={"owner": _owner(ctx)}, timeout=30)
        return list(res.get("jobs") or [])
    uid = ctx.workspace.user_id
    with _jobs_lock:
        mine = [j for j in _jobs.values() if j.org_id == ctx.org_id and j.user_id == uid]
    return [j.summary() for j in sorted(mine, key=lambda j: j.started_at, reverse=True)]


@atexit.register
def _kill_all_jobs() -> None:
    with _jobs_lock:
        running = [j for j in _jobs.values() if j.status == "running" and j.proc is not None]
    for job in running:
        job.kill_requested = True
        try:
            _kill_tree(job.proc)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- discovery
async def which(ctx: ToolContext, names: Sequence[str]) -> dict[str, str | None]:
    """Locate programs on the execution host's PATH."""
    clean = []
    for n in names:
        n = (n or "").strip()
        if not _PROGRAM_NAME.match(n):
            raise ToolError(f"Invalid program name: {n!r}")
        clean.append(n)
    if not clean:
        return {}
    if sandboxed():
        script = 'for n in "$@"; do p=$(command -v "$n" 2>/dev/null); printf "%s\\t%s\\n" "$n" "$p"; done'
        res = await run_command(ctx, ["/bin/sh", "-c", script, "sh", *clean], timeout=20)
        found: dict[str, str | None] = {n: None for n in clean}
        for line in res["stdout"].splitlines():
            name, _, path = line.partition("\t")
            if name in found:
                found[name] = path.strip() or None
        return found
    env, _ = build_env(ctx, None, remote=False)
    return {n: find_program(n, env.get("PATH")) for n in clean}


async def env_names(ctx: ToolContext) -> list[str]:
    """Names (never values) of the environment variables commands receive."""
    if sandboxed():
        res = await run_command(ctx, ["env"], timeout=20)
        names = {line.split("=", 1)[0] for line in res["stdout"].splitlines() if "=" in line}
        return sorted(n for n in names if _ENV_NAME.match(n))
    env, _ = build_env(ctx, None, remote=False)
    return sorted(env)
