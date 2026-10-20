"""Code tools: run Python/JavaScript snippets, test suites, linters and formatters in the user's workspace.

Snippets and tests execute arbitrary code, so their risk is MEDIUM inside the remote sandbox and HIGH on the backend
host. Files a run creates under output/ are registered as artifacts automatically.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Literal

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin._exec import (
    TOOL_TIMEOUT,
    clip,
    exec_risk,
    fence,
    find_program,
    format_result,
    python_exe,
    require_write,
    run_command,
    run_python,
    sandboxed,
    where_label,
    which,
    ws_path,
)

code = ToolGroup(
    "code", "Code",
    "Run Python and JavaScript code, test suites, linters and formatters in your workspace",
    prefix="code", icon="code", category="engineering",
)

MAX_ARTIFACTS_PER_RUN = 20
_SKIP_OUTPUT_DIRS = {"__pycache__", "node_modules", ".git"}


def _code_preview(lang: str, fence_lang: str):
    def preview(args: dict) -> str:
        cwd = args.get("cwd") or "workspace"
        return (f"**Run {lang} code** {where_label()} (cwd `{cwd}/`)\n\n"
                f"{fence(clip(str(args.get('code', '')), 6000), fence_lang)}")

    return preview


def _test_preview(args: dict) -> str:
    extra = f" `{args.get('args')}`" if args.get("args") else ""
    return (f"**Run the test suite** of `{args.get('path')}` ({args.get('framework') or 'auto'}){extra} "
            f"{where_label()}. Tests execute the project's code.")


def _snapshot(root: Path, limit: int = 5000) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _SKIP_OUTPUT_DIRS]
        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(dirpath, name)
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p)] = (st.st_mtime_ns, st.st_size)
            if len(out) >= limit:
                return out
    return out


async def _register_outputs(ctx: ToolContext, before: dict[str, tuple[int, int]]) -> list[str]:
    after = _snapshot(ctx.workspace.user_root / "output")
    changed = [p for p, sig in after.items() if before.get(p) != sig]
    changed.sort(key=lambda p: after[p][0])
    registered: list[str] = []
    for p in changed[:MAX_ARTIFACTS_PER_RUN]:
        vpath = ctx.workspace.virtual(Path(p))
        try:
            await ctx.add_artifact(vpath)
        except Exception:  # noqa: BLE001  (file vanished, etc.)
            continue
        registered.append(vpath)
    return registered


def _with_artifacts(text: str, files: list[str]) -> str:
    if files:
        text += "\nNew/updated files in output/ (attached as artifacts): " + ", ".join(files)
    return text


@code.tool(risk=Risk.HIGH, scope="write", risk_fn=exec_risk, timeout=TOOL_TIMEOUT,
           preview=_code_preview("Python", "python"))
async def python(ctx: ToolContext, code: str, cwd: str = "workspace", timeout: int = 120) -> str:
    """Run a Python 3 snippet in your workspace and return what it prints (stdout/stderr) and its exit code.

    Each call is a fresh interpreter; use print() to see values. pandas, numpy, matplotlib, openpyxl, python-docx
    and python-pptx are available (plus anything installed with shell_install_package). Paths are relative to `cwd`
    (default workspace/): save deliverables to "../output/" (or os.environ["REN_OUTPUT"]); uploaded files are in
    "../uploads/" and the shared drive at os.environ["REN_SHARED"]. New files written to output/ are attached to the
    conversation automatically.

    Args:
        code: Python source code to execute.
        cwd: Workspace folder to run in (it is importable, so local modules work).
        timeout: Seconds before the run is killed (1-600).
    """
    require_write(ctx, "run code")
    before = _snapshot(ctx.workspace.user_root / "output")
    res = await run_python(ctx, code, cwd_virtual=cwd, timeout=timeout)
    files = await _register_outputs(ctx, before)
    return _with_artifacts(format_result(res), files)


@code.tool(risk=Risk.HIGH, scope="write", risk_fn=exec_risk, timeout=TOOL_TIMEOUT,
           preview=_code_preview("JavaScript", "javascript"))
async def javascript(ctx: ToolContext, code: str, cwd: str = "workspace", timeout: int = 120) -> str:
    """Run a JavaScript snippet with Node.js in your workspace and return its output and exit code.

    CommonJS (`require`) works everywhere; ES module syntax (`import`, top-level await) is auto-detected on Node 22+.
    Modules installed with shell_install_package(manager="npm") in `cwd` can be required. New files written to
    output/ (e.g. "../output/data.json") are attached to the conversation automatically.

    Args:
        code: JavaScript source code to execute.
        cwd: Workspace folder to run in.
        timeout: Seconds before the run is killed (1-600).
    """
    require_write(ctx, "run code")
    if not sandboxed() and find_program("node") is None:
        raise ToolError("Node.js is not installed on the Ren server, so JavaScript can't be run here.")
    before = _snapshot(ctx.workspace.user_root / "output")
    res = await run_command(ctx, ["node", "-"], cwd_virtual=cwd, timeout=timeout, stdin=code)
    if res["exit_code"] == 127 and "not found" in res["stderr"].lower():
        raise ToolError("Node.js is not available in the execution environment.")
    files = await _register_outputs(ctx, before)
    return _with_artifacts(format_result(res), files)


def _detect_framework(project: Path) -> str | None:
    pkg = project / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        test_script = str(scripts.get("test") or "")
        if test_script and "no test specified" not in test_script:
            return "npm"
    markers = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py", "setup.py")
    if any((project / m).is_file() for m in markers) or (project / "tests").is_dir():
        return "pytest"
    if any(project.glob("test_*.py")) or any(project.glob("*_test.py")):
        return "pytest"
    return None


@code.tool(risk=Risk.HIGH, scope="write", risk_fn=exec_risk, timeout=TOOL_TIMEOUT, preview=_test_preview)
async def test(ctx: ToolContext, path: str = "workspace", framework: Literal["auto", "pytest", "npm"] = "auto",
               args: str = "", timeout: int = 300) -> str:
    """Run a project's test suite (pytest or `npm test`) and return the results.

    With framework="auto", a package.json with a "test" script means npm, otherwise Python projects use pytest.

    Args:
        path: Project folder, e.g. "projects/my-app".
        framework: "auto", "pytest" or "npm".
        args: Extra arguments, e.g. "-k test_login -x" for pytest or "-- --runInBand" for npm.
        timeout: Seconds before the test run is killed (1-600).
    """
    require_write(ctx, "run tests")
    project = ws_path(ctx, path, must_exist=True)
    if not project.is_dir():
        raise ToolError(f"{ctx.workspace.normalize(path)} is not a folder.")
    vproject = ctx.workspace.virtual(project)
    fw = framework if framework != "auto" else _detect_framework(project)
    if fw is None:
        raise ToolError(f"Couldn't detect a test framework in {vproject} (no package.json test script or pytest "
                        "files). Pass framework='pytest' or 'npm'.")
    try:
        extra = shlex.split(args) if args else []
    except ValueError as e:
        raise ToolError(f"Could not parse args: {e}") from e
    await ctx.progress(f"Running {fw} tests in {vproject}")
    if fw == "npm":
        argv = ["npm", "test", *extra]
        res = await run_command(ctx, argv, cwd_virtual=vproject, timeout=timeout, env={"CI": "1"})
    else:
        argv = [python_exe(), "-m", "pytest", "-q", "--color=no", *extra]
        res = await run_command(ctx, argv, cwd_virtual=vproject, timeout=timeout)
        if res["exit_code"] == 1 and "No module named pytest" in res["stderr"]:
            raise ToolError("pytest is not installed; install it with shell_install_package(['pytest']).")
    return format_result(res, budget=14_000)


async def _python_tool(ctx: ToolContext, module: str, binary: str | None = None) -> list[str] | None:
    """argv prefix for a Python CLI tool: `python -I -m module` (isolated: ignores cwd/PYTHONPATH) or its binary."""
    if sandboxed():
        found = await which(ctx, [binary or module])
        if found.get(binary or module):
            return [binary or module]
        return None
    if importlib.util.find_spec(module) is not None:
        return [sys.executable, "-I", "-m", module]
    path = find_program(binary or module)
    return [path] if path else None


def _target(ctx: ToolContext, path: str) -> tuple[str, str]:
    """(cwd virtual path, target name) for a file or folder."""
    p = ws_path(ctx, path, must_exist=True)
    if p.is_dir():
        return ctx.workspace.virtual(p), "."
    return ctx.workspace.virtual(p.parent), p.name


@code.tool(risk=Risk.LOW, scope="read", timeout=300)
async def lint(ctx: ToolContext, path: str = "workspace") -> str:
    """Lint Python code with ruff (or pyflakes as a fallback) and list the problems found.

    Read-only: nothing is changed and the code is not executed.

    Args:
        path: Python file or folder to check, e.g. "projects/app/src".
    """
    cwd, target = _target(ctx, path)
    ruff = await _python_tool(ctx, "ruff")
    if ruff:
        argv = [*ruff, "check", "--no-cache", "--output-format", "concise", "--", target]
        tool = "ruff"
    else:
        flakes = await _python_tool(ctx, "pyflakes")
        if not flakes:
            raise ToolError("No Python linter is available (install ruff: shell_install_package(['ruff'])).")
        argv, tool = [*flakes, target], "pyflakes"
    res = await run_command(ctx, argv, cwd_virtual=cwd, timeout=240, user_paths=False)
    out = (res["stdout"] + ("\n" + res["stderr"] if res["stderr"].strip() else "")).strip()
    if res["exit_code"] == 0 and (not out or out.startswith("All checks passed")):
        return f"{tool}: no problems found in {ctx.workspace.normalize(path)}."
    return f"{tool} (exit code {res['exit_code']}):\n{clip(out, 14_000)}"


@code.tool(name="format", risk=Risk.MEDIUM, scope="write", timeout=300)
async def format_(ctx: ToolContext, path: str, check: bool = False) -> str:
    """Format Python code in place with ruff format (or black), or only report what would change.

    Args:
        path: Python file or folder to format, e.g. "projects/app".
        check: Only show the diff of what would change, without modifying files.
    """
    if not check:
        require_write(ctx, "format files")
    cwd, target = _target(ctx, path)
    ruff = await _python_tool(ctx, "ruff")
    if ruff:
        argv = [*ruff, "format", "--no-cache", *(["--diff"] if check else []), "--", target]
        tool = "ruff format"
    else:
        black = await _python_tool(ctx, "black")
        if not black:
            raise ToolError("No Python formatter is available (install ruff: shell_install_package(['ruff'])).")
        argv, tool = [*black, *(["--diff", "--check"] if check else []), "--", target], "black"
    res = await run_command(ctx, argv, cwd_virtual=cwd, timeout=240, user_paths=False)
    out = (res["stdout"] + ("\n" + res["stderr"] if res["stderr"].strip() else "")).strip()
    if check and res["exit_code"] == 0:
        return f"{tool}: already formatted ({out or 'no changes'})."
    return f"{tool} (exit code {res['exit_code']}):\n{clip(out, 14_000) or '(no output)'}"
