"""Git tools for repositories inside the user's workspace (typically projects/<repo>), via the git CLI.

Safety: every invocation disables hooks, fsmonitor and external diff/textconv programs, never prompts, and stops
repository discovery at the workspace boundary (GIT_CEILING_DIRECTORIES) so a folder that isn't a repo can never
resolve to a repository outside the sandbox. HTTPS access to GitHub uses the org's `github` connector token through
a transient credential helper that reads it from the environment (the token never appears in argv or output).
"""
from __future__ import annotations

import os
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from app.services.workspace import TOP_DIRS
from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin._exec import (
    TOOL_TIMEOUT,
    clip,
    require_write,
    run_command,
    sandboxed,
    ws_path,
)
from app.tools.net import assert_public_url

git = ToolGroup(
    "git", "Git",
    "Version control for repositories in your workspace: status, diffs, history, branches, commits, pull/push, clone",
    prefix="git", icon="git-branch", category="engineering",
)

PROTECTED_BRANCHES = {"main", "master", "trunk", "production", "prod", "release", "stable"}
_NO_HOOKS = str(Path(tempfile.gettempdir()) / f"ren-no-hooks-{secrets.token_hex(8)}")  # never created
_SAFE_CONFIG = [
    "-c", "core.fsmonitor=false",
    "-c", f"core.hooksPath={_NO_HOOKS}",
    "-c", "protocol.ext.allow=never",
    "-c", "safe.directory=*",
    "-c", "core.quotepath=false",
    "-c", "color.ui=false",
    "-c", "advice.detachedHead=false",
]
_REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/~^@{}:+-]*$")
_BRANCH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/+-]*$")
_REMOTE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_OUT_BUDGET = 14_000


# --------------------------------------------------------------------------- helpers
def _check_ref(ref: str) -> str:
    """Commit-ish or range (never starts with '-', so it can't be parsed as an option)."""
    ref = (ref or "").strip()
    if not ref or not _REF.match(ref):
        raise ToolError(f"Invalid ref: {ref!r}")
    return ref


def _check_branch(name: str) -> str:
    name = (name or "").strip()
    if (not _BRANCH.match(name) or ".." in name or "//" in name or name.endswith((".lock", "/", "."))
            or name == "HEAD"):
        raise ToolError(f"Invalid branch name: {name!r}")
    return name


def _check_remote(name: str) -> str:
    name = (name or "").strip()
    if not _REMOTE.match(name):
        raise ToolError(f"Invalid remote name: {name!r}")
    return name


def _check_pathspec(path: str) -> str:
    path = (path or "").strip().replace("\\", "/")
    if not path or path.startswith("-") or "\x00" in path:
        raise ToolError(f"Invalid path: {path!r}")
    return path


def _repo(ctx: ToolContext, repo: str) -> str:
    """Virtual path of the repository root containing `repo` (walks up to the workspace boundary)."""
    start = ws_path(ctx, repo, must_exist=True)
    if not start.is_dir():
        start = start.parent
    boundaries = {ctx.workspace.user_root, ctx.workspace.shared_root}
    p = start
    while True:
        if (p / ".git").exists():
            return ctx.workspace.virtual(p)
        if p in boundaries or p.parent == p:
            break
        p = p.parent
    raise ToolError(f"{ctx.workspace.normalize(repo)} is not inside a git repository. Use git_init or git_clone "
                    "(repositories usually live in projects/).")


def _git_env(ctx: ToolContext) -> dict[str, str]:
    sep = ":" if sandboxed() else os.pathsep
    return {
        "GIT_CEILING_DIRECTORIES": sep.join([str(ctx.workspace.user_root), str(ctx.workspace.shared_root)]),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_EDITOR": "true",
        "GIT_SEQUENCE_EDITOR": "true",
        "GIT_PAGER": "cat",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
        "GIT_OPTIONAL_LOCKS": "0",
    }


async def _run(ctx: ToolContext, cwd: str, args: list[str], *, timeout: int = 60, config: list[str] | None = None,
               env: dict | None = None, redact: list[str] | None = None, check: bool = True) -> dict:
    full_env = {**_git_env(ctx), **(env or {})}
    res = await run_command(ctx, ["git", *_SAFE_CONFIG, *(config or []), *args], cwd_virtual=cwd, timeout=timeout,
                            env=full_env, user_paths=False, redact=redact or [])
    if res["exit_code"] == 127 and "not found" in res["stderr"].lower():
        raise ToolError("git is not installed in the execution environment.")
    if check and res["exit_code"] != 0:
        msg = (res["stderr"].strip() or res["stdout"].strip() or "no output").replace("\r\n", "\n")
        if res.get("timed_out"):
            msg = f"timed out after {timeout}s. {msg}"
        raise ToolError(f"git {args[0]} failed (exit {res['exit_code']}): {clip(msg, 4000)}")
    return res


def _text(res: dict, empty: str = "(no output)") -> str:
    out = res["stdout"].replace("\r\n", "\n").strip("\n")
    err = res["stderr"].replace("\r\n", "\n").strip("\n")
    text = "\n".join(x for x in (out, err) if x) or empty
    return clip(text, _OUT_BUDGET, tail_ratio=0.3)


def _host_of(url: str) -> tuple[str, str] | None:
    """(scheme, host) for http(s)/ssh/scp-like git URLs; None for local paths and unsupported transports."""
    url = url.strip()
    if "::" in url.split("/", 1)[0]:  # "<transport>::<address>" remote helpers (e.g. ext::) are never allowed
        return None
    m = re.match(r"^(?P<user>[A-Za-z0-9._-]+@)?(?P<host>[A-Za-z0-9.-]+):(?!//)(?P<path>.+)$", url)
    if m and not re.match(r"^[A-Za-z]:[\\/]", url):  # scp-like user@host:path (but not C:\path)
        return "ssh", m.group("host").lower()
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https", "ssh", "git") and parsed.hostname:
        return parsed.scheme, parsed.hostname.lower()
    return None


async def _check_network_url(url: str) -> tuple[str, str]:
    info = _host_of(url)
    if info is None:
        raise ToolError("Only remote https:// (or ssh) git URLs are allowed; local paths and file:// are not.")
    scheme, host = info
    if scheme in ("http", "https"):
        await assert_public_url(url)
    else:
        await assert_public_url(f"https://{host}/")  # same private-network policy for ssh/git hosts
    return scheme, host


async def _auth(ctx: ToolContext, url: str) -> tuple[list[str], dict[str, str], list[str]]:
    """git -c args + env that authenticate HTTPS requests to GitHub with the org's connector token."""
    info = _host_of(url)
    if info is None or info[0] not in ("http", "https"):
        return [], {}, []
    scheme, host = info
    try:
        cfg = await ctx.connector("github")
    except ToolError:
        return [], {}, []
    token = str(cfg.get("token") or cfg.get("access_token") or cfg.get("api_key") or "").strip()
    if not token:
        return [], {}, []
    hosts = {"github.com"}
    for key in ("host", "base_url", "api_url", "url"):
        value = str(cfg.get(key) or "").strip()
        if value:
            h = urlparse(value if "://" in value else f"https://{value}").hostname
            if h:
                hosts.add(h.lower().removeprefix("api."))
    if host not in hosts:
        return [], {}, []
    helper = '!f() { test "$1" = get || exit 0; echo username=x-access-token; echo "password=$REN_GIT_TOKEN"; }; f'
    config = ["-c", "credential.helper=", "-c", f"credential.{scheme}://{host}.helper={helper}",
              "-c", "credential.useHttpPath=false"]
    return config, {"REN_GIT_TOKEN": token}, [token]


async def _remote_url(ctx: ToolContext, repo: str, remote: str) -> str:
    res = await _run(ctx, repo, ["remote", "get-url", remote], check=False)
    url = res["stdout"].strip()
    if res["exit_code"] != 0 or not url:
        raise ToolError(f"Remote '{remote}' is not configured in {repo} (git remote -v shows the remotes).")
    return url


async def _acting_identity(ctx: ToolContext) -> tuple[str, str]:
    name, email = "Ren Agent", "ren-agent@users.noreply.ren.local"
    if ctx.user_id and not ctx.extra.get("no_db"):
        try:
            from app.models import User

            async with ctx.session() as db:
                user = await db.get(User, ctx.user_id)
            if user is not None:
                name = (getattr(user, "name", None) or name).strip() or name
                email = (getattr(user, "email", None) or email).strip() or email
        except Exception:  # noqa: BLE001
            pass
    return name, email


async def _identity_env(ctx: ToolContext, repo: str) -> dict[str, str]:
    """Author/committer env vars when the repository has no configured git identity."""
    res = await _run(ctx, repo, ["config", "--get-regexp", r"^user\.(name|email)$"], check=False)
    keys = {line.split(" ", 1)[0] for line in res["stdout"].splitlines() if " " in line}
    if {"user.name", "user.email"} <= keys:
        return {}
    name, email = await _acting_identity(ctx)
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email}


def _target_dir(ctx: ToolContext, directory: str) -> str:
    raw = (directory or "").replace("\\", "/").strip()
    while raw.startswith("./"):
        raw = raw[2:]
    raw = raw.lstrip("/")
    first = raw.split("/", 1)[0] if raw else ""
    # bare names go to projects/, not workspace/
    vp = ctx.workspace.normalize(raw if first in TOP_DIRS else f"projects/{raw}")
    vp = "/".join(seg for seg in vp.split("/") if seg not in ("", "."))
    if vp in TOP_DIRS or vp == "":
        raise ToolError("Give a folder name for the repository, e.g. 'my-app' (created under projects/).")
    if ".git" in vp.split("/"):
        raise ToolError("Invalid repository folder.")
    return vp


# --------------------------------------------------------------------------- read-only tools
@git.tool(risk=Risk.LOW, scope="read")
async def status(ctx: ToolContext, repo: str) -> str:
    """Show the working-tree status of a repository: current branch, ahead/behind, staged/modified/untracked files.

    Args:
        repo: Workspace path of the repository (or any folder inside it), e.g. "projects/my-app".
    """
    r = _repo(ctx, repo)
    res = await _run(ctx, r, ["status", "--short", "--branch", "--untracked-files=normal"])
    text = _text(res)
    if len(text.splitlines()) <= 1:
        text += "\n(working tree clean)"
    return f"[{r}]\n{text}"


@git.tool(risk=Risk.LOW, scope="read")
async def log(ctx: ToolContext, repo: str, max_count: int = 20, ref: str | None = None,
              path: str | None = None) -> str:
    """Show recent commits (short hash, date, author, subject, branch/tag names).

    Args:
        repo: Workspace path of the repository, e.g. "projects/my-app".
        max_count: Number of commits to show (1-200).
        ref: Branch, tag, commit or range to show, e.g. "main" or "main..feature" (default: current branch).
        path: Only commits touching this file or folder (relative to the repository root).
    """
    r = _repo(ctx, repo)
    args = ["log", f"-n{max(1, min(max_count, 200))}", "--date=short", "--pretty=format:%h %ad %an: %s%d"]
    if ref:
        args.append(_check_ref(ref))
    if path:
        args += ["--", _check_pathspec(path)]
    res = await _run(ctx, r, args, check=False)
    if res["exit_code"] != 0 and "does not have any commits" in res["stderr"]:
        return f"[{r}] No commits yet."
    if res["exit_code"] != 0:
        raise ToolError(f"git log failed: {clip(res['stderr'].strip(), 2000)}")
    return _text(res, "No commits match.")


@git.tool(risk=Risk.LOW, scope="read")
async def diff(ctx: ToolContext, repo: str, staged: bool = False, ref: str | None = None, path: str | None = None,
               stat: bool = False, context: int = 3) -> str:
    """Show changes as a unified diff: unstaged changes by default, staged ones with staged=true, or vs a ref.

    Args:
        repo: Workspace path of the repository, e.g. "projects/my-app".
        staged: Show changes staged for the next commit (git diff --cached).
        ref: Compare against this commit/branch, or a range like "main...feature".
        path: Limit to this file or folder (relative to the repository root).
        stat: Only show a per-file summary of changed lines.
        context: Context lines around each change (0-20).
    """
    r = _repo(ctx, repo)
    args = ["diff", "--no-ext-diff", "--no-textconv", f"-U{max(0, min(context, 20))}"]
    if staged:
        args.append("--cached")
    if stat:
        args.append("--stat")
    if ref:
        args.append(_check_ref(ref))
    if path:
        args += ["--", _check_pathspec(path)]
    res = await _run(ctx, r, args)
    return _text(res, "No differences.")


@git.tool(risk=Risk.LOW, scope="read")
async def show(ctx: ToolContext, repo: str, ref: str = "HEAD", path: str | None = None, stat: bool = False) -> str:
    """Show a commit (message and diff), or the content of a file as of a commit when `path` is given.

    Args:
        repo: Workspace path of the repository, e.g. "projects/my-app".
        ref: Commit, branch or tag (default HEAD).
        path: File path relative to the repository root; shows that file's content at `ref`.
        stat: For commits, show only the per-file summary instead of the full diff.
    """
    r = _repo(ctx, repo)
    ref = _check_ref(ref)
    if path:
        res = await _run(ctx, r, ["show", "--no-textconv", f"{ref}:{_check_pathspec(path).lstrip('/')}"])
    else:
        args = ["show", "--no-ext-diff", "--no-textconv", "--date=iso"]
        if stat:
            args.append("--stat")
        res = await _run(ctx, r, [*args, ref])
    return _text(res)


@git.tool(risk=Risk.LOW, scope="read")
async def branch(ctx: ToolContext, repo: str, all: bool = False) -> str:  # noqa: A002
    """List branches with their latest commit and upstream tracking info (* marks the current branch).

    Args:
        repo: Workspace path of the repository, e.g. "projects/my-app".
        all: Include remote-tracking branches.
    """
    r = _repo(ctx, repo)
    res = await _run(ctx, r, ["branch", "-vv", *(["-a"] if all else [])])
    return _text(res, "No branches yet (make a first commit).")


# --------------------------------------------------------------------------- local changes
@git.tool(risk=Risk.MEDIUM, scope="write")
async def checkout(ctx: ToolContext, repo: str, ref: str) -> str:
    """Switch the working tree to a branch, tag or commit. Uncommitted changes are kept; git refuses if they'd be lost.

    Args:
        repo: Workspace path of the repository, e.g. "projects/my-app".
        ref: Branch, tag or commit to check out, e.g. "main" or "feature/login".
    """
    require_write(ctx, "change repositories")
    r = _repo(ctx, repo)
    res = await _run(ctx, r, ["checkout", _check_ref(ref), "--"])
    return _text(res, f"Switched to {ref}.")


@git.tool(risk=Risk.MEDIUM, scope="write")
async def create_branch(ctx: ToolContext, repo: str, name: str, start_point: str | None = None,
                        checkout: bool = True) -> str:
    """Create a new branch (from the current commit or `start_point`) and optionally switch to it.

    Args:
        repo: Workspace path of the repository, e.g. "projects/my-app".
        name: New branch name, e.g. "feature/csv-export".
        start_point: Commit, branch or tag to start from (default: current HEAD).
        checkout: Switch to the new branch after creating it.
    """
    require_write(ctx, "change repositories")
    r = _repo(ctx, repo)
    name = _check_branch(name)
    start = [_check_ref(start_point)] if start_point else []
    args = ["switch", "-c", name, *start] if checkout else ["branch", name, *start]
    res = await _run(ctx, r, args)
    return _text(res, f"Created branch {name}" + (" and switched to it." if checkout else "."))


@git.tool(risk=Risk.MEDIUM, scope="write")
async def add(ctx: ToolContext, repo: str, paths: list[str] | None = None) -> str:
    """Stage files for the next commit.

    Args:
        repo: Workspace path of the repository, e.g. "projects/my-app".
        paths: Files or folders relative to the repository root (default ["."] = everything, incl. deletions).
    """
    require_write(ctx, "change repositories")
    r = _repo(ctx, repo)
    specs = [_check_pathspec(p) for p in (paths or ["."])]
    await _run(ctx, r, ["add", "-A", "--", *specs])
    res = await _run(ctx, r, ["status", "--short"])
    return "Staged. Current status:\n" + _text(res, "(nothing to commit)")


@git.tool(risk=Risk.MEDIUM, scope="write")
async def commit(ctx: ToolContext, repo: str, message: str, add_all: bool = False) -> str:
    """Record staged changes as a commit (stage files first with git_add, or set add_all=true).

    The author is the repository's configured git identity, or else the Ren user you're acting for.

    Args:
        repo: Workspace path of the repository, e.g. "projects/my-app".
        message: Commit message: a short summary line, optionally followed by a blank line and details.
        add_all: Stage all changes (new, modified, deleted files) before committing.
    """
    require_write(ctx, "change repositories")
    r = _repo(ctx, repo)
    if not message.strip():
        raise ToolError("A commit message is required.")
    if add_all:
        await _run(ctx, r, ["add", "-A"])
    env = await _identity_env(ctx, r)
    res = await _run(ctx, r, ["commit", "-m", message], env=env, check=False)
    if res["exit_code"] != 0:
        out = (res["stdout"] + "\n" + res["stderr"]).strip()
        if "nothing to commit" in out or "no changes added" in out:
            raise ToolError("Nothing to commit: stage changes with git_add (or pass add_all=true) first.")
        raise ToolError(f"git commit failed: {clip(out, 3000)}")
    return _text(res)


def _merge_preview(args: dict) -> str:
    if args.get("abort"):
        return f"**Abort the merge in progress** in `{args.get('repo')}`"
    extra = " (always create a merge commit)" if args.get("no_ff") else ""
    return f"**Merge** branch `{args.get('branch')}` into the current branch of `{args.get('repo')}`{extra}"


@git.tool(risk=Risk.HIGH, scope="write", preview=_merge_preview)
async def merge(ctx: ToolContext, repo: str, branch: str = "", no_ff: bool = False, message: str | None = None,
                abort: bool = False) -> str:
    """Merge another branch into the current branch (or abort an in-progress merge with abort=true).

    On conflicts the merge stops and the conflicting files are listed; resolve them and commit, or abort.

    Args:
        repo: Workspace path of the repository, e.g. "projects/my-app".
        branch: Branch (or commit) to merge into the current branch.
        no_ff: Always create a merge commit, even when a fast-forward is possible.
        message: Custom merge commit message.
        abort: Abort the merge currently in progress instead of starting one.
    """
    require_write(ctx, "change repositories")
    r = _repo(ctx, repo)
    if abort:
        res = await _run(ctx, r, ["merge", "--abort"])
        return _text(res, "Merge aborted; the branch is back to its previous state.")
    if not branch:
        raise ToolError("Pass the branch to merge (or abort=true).")
    args = ["merge", "--no-edit", *(["--no-ff"] if no_ff else []), *(["-m", message] if message else []),
            _check_ref(branch)]
    env = await _identity_env(ctx, r)
    res = await _run(ctx, r, args, env=env, check=False)
    if res["exit_code"] != 0:
        out = _text(res)
        if "CONFLICT" in out or "Automatic merge failed" in out:
            return (f"Merge stopped with conflicts:\n{out}\n\nResolve the conflicted files and commit, or call "
                    "git_merge(abort=true).")
        raise ToolError(f"git merge failed: {out}")
    return _text(res)


# --------------------------------------------------------------------------- network
@git.tool(risk=Risk.MEDIUM, scope="write", timeout=TOOL_TIMEOUT)
async def pull(ctx: ToolContext, repo: str, remote: str = "origin", branch: str | None = None,
               rebase: bool = False) -> str:
    """Fetch and integrate changes from a remote (fast-forward only by default, so local work is never merged
    implicitly).

    Args:
        repo: Workspace path of the repository, e.g. "projects/my-app".
        remote: Remote name (default "origin").
        branch: Remote branch to pull (default: the current branch's upstream).
        rebase: Rebase local commits on top of the remote branch instead of requiring a fast-forward.
    """
    require_write(ctx, "change repositories")
    r = _repo(ctx, repo)
    remote = _check_remote(remote)
    url = await _remote_url(ctx, r, remote)
    await _check_network_url(url)
    config, env, secret = await _auth(ctx, url)
    env["GIT_ALLOW_PROTOCOL"] = "https:http:ssh"
    args = ["pull", "--rebase" if rebase else "--ff-only", remote, *([_check_branch(branch)] if branch else [])]
    await ctx.progress(f"Pulling {remote} into {r}")
    res = await _run(ctx, r, args, timeout=600, config=config, env=env, redact=secret)
    return _text(res)


def _push_risk(args: dict) -> Risk:
    branch = str(args.get("branch") or "").strip()
    if args.get("force") or branch in PROTECTED_BRANCHES or branch.startswith(("release/", "hotfix/")):
        return Risk.CRITICAL
    return Risk.HIGH


def _push_preview(args: dict) -> str:
    branch = args.get("branch")
    lines = [f"**Push** branch `{branch}` of `{args.get('repo')}` to remote `{args.get('remote') or 'origin'}`"]
    if args.get("force"):
        lines.append("\n⚠️ **Force push** (with lease): remote history may be overwritten.")
    if str(branch) in PROTECTED_BRANCHES:
        lines.append(f"\n⚠️ `{branch}` is usually a protected branch.")
    return "\n".join(lines)


@git.tool(risk=Risk.HIGH, scope="write", timeout=TOOL_TIMEOUT, risk_fn=_push_risk, preview=_push_preview)
async def push(ctx: ToolContext, repo: str, branch: str, remote: str = "origin", force: bool = False,
               set_upstream: bool = True) -> str:
    """Push a local branch to a remote repository (e.g. GitHub), making the commits visible to others.

    Args:
        repo: Workspace path of the repository, e.g. "projects/my-app".
        branch: Local branch to push; it is pushed to the remote branch of the same name.
        remote: Remote name (default "origin").
        force: Overwrite the remote branch history (uses --force-with-lease). Rarely appropriate.
        set_upstream: Record the remote branch as the upstream of the local branch.
    """
    require_write(ctx, "push changes")
    r = _repo(ctx, repo)
    branch, remote = _check_branch(branch), _check_remote(remote)
    url = await _remote_url(ctx, r, remote)
    await _check_network_url(url)
    config, env, secret = await _auth(ctx, url)
    env["GIT_ALLOW_PROTOCOL"] = "https:http:ssh"
    args = ["push", *(["--set-upstream"] if set_upstream else []), *(["--force-with-lease"] if force else []),
            remote, f"{branch}:{branch}"]
    await ctx.progress(f"Pushing {branch} to {remote}")
    res = await _run(ctx, r, args, timeout=600, config=config, env=env, redact=secret, check=False)
    if res["exit_code"] != 0:
        out = _text(res)
        if "Authentication failed" in out or "could not read Username" in out or "403" in out:
            out += ("\nAuthentication failed: for GitHub, an admin can connect the GitHub integration (Connectors) "
                    "with a token that has push access.")
        raise ToolError(f"git push failed: {out}")
    return _text(res)


@git.tool(risk=Risk.MEDIUM, scope="write", timeout=TOOL_TIMEOUT)
async def clone(ctx: ToolContext, url: str, directory: str | None = None, branch: str | None = None,
                depth: int | None = None) -> str:
    """Clone a remote repository (https URL) into your projects/ folder.

    Private GitHub repositories work when the GitHub integration is connected.

    Args:
        url: Repository URL, e.g. "https://github.com/org/repo.git".
        directory: Folder name under projects/ (default: the repository name).
        branch: Branch or tag to check out instead of the default branch.
        depth: Create a shallow clone with only this many recent commits (faster for large repos).
    """
    require_write(ctx, "clone repositories")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ToolError("Only https:// repository URLs can be cloned (e.g. https://github.com/org/repo.git).")
    if parsed.username or parsed.password:
        raise ToolError("Don't put credentials in the URL; connect the GitHub integration instead.")
    await assert_public_url(url)
    name = re.sub(r"[^A-Za-z0-9._-]", "-", Path(parsed.path.rstrip("/")).name.removesuffix(".git")) or "repo"
    vp = _target_dir(ctx, directory or name)
    target = ws_path(ctx, vp)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ToolError(f"{vp} already exists and is not empty; choose another directory.")
    parent_v, folder = vp.rsplit("/", 1)
    ws_path(ctx, parent_v).mkdir(parents=True, exist_ok=True)
    config, env, secret = await _auth(ctx, url)
    env["GIT_ALLOW_PROTOCOL"] = "https:http"
    args = ["clone", "--no-recurse-submodules"]
    if branch:
        args += ["--branch", _check_branch(branch)]
    if depth:
        args += ["--depth", str(max(1, min(int(depth), 100_000)))]
    args += ["--", url, folder]
    await ctx.progress(f"Cloning {url}")
    res = await _run(ctx, parent_v, args, timeout=600, config=config, env=env, redact=secret, check=False)
    if res["exit_code"] != 0:
        out = _text(res)
        if "could not read Username" in out or "Authentication failed" in out:
            out += "\nThe repository may be private: an admin can connect the GitHub integration in Connectors."
        raise ToolError(f"git clone failed: {out}")
    head = await _run(ctx, vp, ["log", "-1", "--pretty=format:%h %s (%ad)", "--date=short"], check=False)
    summary = head["stdout"].strip() or "empty repository"
    return f"Cloned {url} into {vp}/ — HEAD: {summary}"


@git.tool(risk=Risk.MEDIUM, scope="write")
async def init(ctx: ToolContext, directory: str, initial_branch: str = "main") -> str:
    """Create a new, empty git repository (the folder is created if needed; bare names go under projects/).

    Args:
        directory: Folder for the repository, e.g. "my-app" (→ projects/my-app) or "projects/my-app".
        initial_branch: Name of the first branch.
    """
    require_write(ctx, "create repositories")
    vp = _target_dir(ctx, directory)
    target = ws_path(ctx, vp)
    if (target / ".git").exists():
        raise ToolError(f"{vp} is already a git repository.")
    if target.exists() and not target.is_dir():
        raise ToolError(f"{vp} is a file.")
    initial_branch = _check_branch(initial_branch)
    target.mkdir(parents=True, exist_ok=True)
    await _run(ctx, vp, ["init", "-b", initial_branch])
    return f"Initialized an empty git repository in {vp}/ (branch {initial_branch})."
