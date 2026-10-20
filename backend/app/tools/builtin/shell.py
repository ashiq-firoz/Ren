"""Shell tools: run commands (foreground or background) and install packages in the user's workspace.

Execution goes through ``_exec``: the remote sandbox when ``settings.sandbox_url`` is set (risk MEDIUM), otherwise the
backend host (risk HIGH). Commands always start in a workspace folder with a minimal environment.
"""
from __future__ import annotations

import importlib.util
import platform
import re
import sys
from typing import Literal

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin._exec import (
    DEFAULT_JOB_TIMEOUT,
    MAX_JOB_TIMEOUT,
    TOOL_TIMEOUT,
    clip,
    env_names,
    exec_mode,
    exec_risk,
    fence,
    find_program,
    format_result,
    kill_job,
    list_jobs,
    poll_job,
    require_write,
    run_command,
    sandboxed,
    shell_label,
    start_job,
    user_pylib,
    where_label,
)
from app.tools.builtin._exec import which as _which

sh = ToolGroup(
    "shell", "Shell",
    "Run shell commands and background processes, and install pip/npm packages, in your workspace",
    prefix="shell", icon="terminal", category="engineering",
)

_PIP_SPEC = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?"
    r"((===|==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]+(,(===|==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]+)*)?$"
)
_NPM_SPEC = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(@[A-Za-z0-9.^~<>=*+_-]+)?$")


def _command_preview(title: str):
    def preview(args: dict) -> str:
        cwd = args.get("cwd") or "workspace"
        timeout = args.get("timeout")
        lines = [f"**{title}** {where_label()} in `{cwd}/`", "", fence(clip(str(args.get("command", "")), 4000),
                                                                         "bash")]
        if timeout:
            lines.append(f"Timeout: {timeout}s")
        return "\n".join(lines)

    return preview


def _install_preview(args: dict) -> str:
    manager = args.get("manager") or "pip"
    pkgs = ", ".join(f"`{p}`" for p in (args.get("packages") or []))
    where = "your private Python library" if manager == "pip" else f"`{args.get('cwd') or 'workspace'}/node_modules`"
    return (f"**Install {manager} packages** {where_label()} into {where}:\n\n{pkgs}\n\n"
            "Package installers download and run third-party code.")


@sh.tool(name="exec", risk=Risk.HIGH, scope="write", risk_fn=exec_risk, timeout=TOOL_TIMEOUT,
         preview=_command_preview("Run a shell command"))
async def exec_(ctx: ToolContext, command: str, cwd: str = "workspace", timeout: int = 120) -> str:
    """Run a shell command in your workspace and return its exit code, stdout and stderr.

    Runs bash (Windows PowerShell on Windows hosts without Git Bash) with `cwd` as the working directory. stdin is
    closed, so interactive prompts fail; pass non-interactive flags (e.g. `-y`). Long output keeps its beginning and
    end. For servers, watchers or anything slower than the timeout, use shell_exec_background.

    Args:
        command: Command line, e.g. "ls -la", "python script.py --help" or "npm run build".
        cwd: Workspace folder to run in, e.g. "workspace" or "projects/app".
        timeout: Seconds before the command and its child processes are killed (1-600).
    """
    require_write(ctx, "run commands")
    await ctx.progress(f"Running: {clip(command, 80)}")
    res = await run_command(ctx, command, cwd_virtual=cwd, timeout=timeout)
    return format_result(res)


@sh.tool(risk=Risk.HIGH, scope="write", risk_fn=exec_risk, preview=_command_preview("Start a background command"))
async def exec_background(ctx: ToolContext, command: str, cwd: str = "workspace",
                          timeout: int = DEFAULT_JOB_TIMEOUT) -> dict:
    """Start a long-running shell command in the background (dev server, watcher, long build) and return a job id.

    Use shell_poll to read its new output and status, and shell_kill to stop it. At most 5 jobs per user run at once;
    each is killed automatically after `timeout` seconds.

    Args:
        command: Command line, e.g. "npm run dev" or "python train.py".
        cwd: Workspace folder to run in, e.g. "projects/app".
        timeout: Maximum run time in seconds before the job is killed (1-14400).
    """
    require_write(ctx, "run commands")
    timeout = max(1, min(timeout, MAX_JOB_TIMEOUT))
    job = await start_job(ctx, command, cwd_virtual=cwd, timeout=timeout)
    job["hint"] = f"Poll with shell_poll(job_id='{job.get('job_id')}')."
    return job


@sh.tool(risk=Risk.LOW, scope="read")
async def poll(ctx: ToolContext, job_id: str, all_output: bool = False, max_chars: int = 10000) -> dict:
    """Check a background job: status, exit code and the output produced since the previous poll.

    Args:
        job_id: Id returned by shell_exec_background.
        all_output: Return all retained output (the last ~64k characters) instead of only new output.
        max_chars: Maximum characters of output to return (200-30000); call again to read more.
    """
    return await poll_job(ctx, job_id, max_chars=max_chars, all_output=all_output)


@sh.tool(risk=Risk.MEDIUM, scope="write")
async def kill(ctx: ToolContext, job_id: str) -> dict:
    """Stop one of your background jobs (and its child processes).

    Args:
        job_id: Id returned by shell_exec_background (see shell_processes).
    """
    require_write(ctx, "stop processes")
    return await kill_job(ctx, job_id)


@sh.tool(risk=Risk.LOW, scope="read")
async def processes(ctx: ToolContext) -> dict:
    """List your background jobs (running and recently finished) with their status and runtime."""
    jobs = await list_jobs(ctx)
    return {"jobs": jobs, "running": sum(1 for j in jobs if j.get("status") == "running")}


@sh.tool(risk=Risk.LOW, scope="read")
async def which(ctx: ToolContext, names: list[str]) -> dict:
    """Check whether programs are installed on the execution host and where (like `which`).

    Args:
        names: Program names, e.g. ["git", "node", "ffmpeg"].
    """
    if not names:
        raise ToolError("Pass at least one program name.")
    if len(names) > 30:
        raise ToolError("Check at most 30 programs at once.")
    found = await _which(ctx, names)
    return {"mode": exec_mode(), "programs": found}


@sh.tool(risk=Risk.LOW, scope="read")
async def env(ctx: ToolContext) -> dict:
    """Describe the command environment: where commands run, which shell, and the names (never the values) of the
    environment variables available to commands."""
    names = await env_names(ctx)
    return {
        "mode": exec_mode(),
        "where": where_label(),
        "shell": shell_label(),
        "host_os": "linux (sandbox)" if sandboxed() else f"{platform.system()} {platform.release()}",
        "variables": names,
        "ren_variables": {
            "REN_WORKSPACE": "absolute path of your private workspace root (contains workspace/, output/, …)",
            "REN_OUTPUT": "absolute path of output/ (deliverables shown to the user)",
            "REN_SHARED": "absolute path of the shared drive (shared/)",
        },
    }


@sh.tool(risk=Risk.HIGH, scope="write", timeout=TOOL_TIMEOUT, preview=_install_preview)
async def install_package(ctx: ToolContext, packages: list[str], manager: Literal["pip", "npm"] = "pip",
                          cwd: str = "workspace", timeout: int = 300) -> str:
    """Install Python (pip) or Node.js (npm) packages into your workspace.

    pip packages go to a private per-user library that shell commands, code_python and code_test pick up
    automatically; npm packages are installed into node_modules of `cwd` (a package.json is created if missing).

    Args:
        packages: Package specifiers, e.g. ["requests", "pandas>=2.2"] or ["lodash@4", "@types/node"].
        manager: "pip" for Python packages, "npm" for Node.js packages.
        cwd: Project folder for npm installs (ignored for pip).
        timeout: Seconds before the installation is aborted (1-600).
    """
    require_write(ctx, "install packages")
    pkgs = [p.strip() for p in packages if p and p.strip()]
    if not pkgs:
        raise ToolError("Pass at least one package name.")
    if len(pkgs) > 25:
        raise ToolError("Install at most 25 packages at once.")
    rx = _PIP_SPEC if manager == "pip" else _NPM_SPEC
    bad = [p for p in pkgs if not rx.match(p)]
    if bad:
        raise ToolError(f"Invalid {manager} package specifier(s): {', '.join(bad)}. Use plain names with optional "
                        "versions (URLs, paths and options are not allowed).")
    await ctx.progress(f"Installing {', '.join(pkgs)} with {manager}")
    if manager == "npm":
        argv = ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error", *pkgs]
        res = await run_command(ctx, argv, cwd_virtual=cwd, timeout=timeout, user_paths=False)
        return format_result(res)
    target = user_pylib(ctx)
    target.mkdir(parents=True, exist_ok=True)
    pip_args = ["install", "--upgrade", "--target", str(target), *pkgs]
    if sandboxed():
        argv = ["python", "-m", "pip", "--no-input", "--disable-pip-version-check", *pip_args]
    elif importlib.util.find_spec("pip") is not None:
        argv = [sys.executable, "-m", "pip", "--no-input", "--disable-pip-version-check", *pip_args]
    elif uv := find_program("uv"):
        argv = [uv, "pip", "install", "--python", sys.executable, "--upgrade", "--target", str(target), *pkgs]
    else:
        raise ToolError("pip is not available in the server's Python environment (and `uv` was not found). Ask an "
                        "admin to install pip, or configure the execution sandbox.")
    res = await run_command(ctx, argv, cwd_virtual="workspace", timeout=timeout, user_paths=False)
    text = format_result(res)
    if res["exit_code"] == 0:
        text += "\nInstalled into your private Python library (available to shell, code_python and code_test)."
    return text
