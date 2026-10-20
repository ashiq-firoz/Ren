"""GitHub tools (REST v3 with a personal access token): code / repository search, issues, pull requests (with diff
stats and checks), reviews, merges, Actions runs and logs, releases and tags. Supports github.com and GitHub
Enterprise Server (`base_url`).
"""
from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote, urlparse

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin import _http as H
from app.tools.builtin._oauth import bearer
from app.tools.connectors import ConnectorField, ConnectorKindSpec, register_connector_kind

DEFAULT_API = "https://api.github.com"
_TS_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ", re.M)


def api_base(cfg: dict[str, Any]) -> str:
    base = str(cfg.get("base_url") or "").strip().rstrip("/") or DEFAULT_API
    parsed = urlparse(base)
    host = parsed.hostname or ""
    if host == "api.github.com" or host.startswith("api.") or parsed.path.rstrip("/").endswith("/api/v3"):
        return base
    if host == "github.com":
        return DEFAULT_API
    return base + "/api/v3"  # GitHub Enterprise Server


def gh_client(cfg: dict[str, Any]) -> H.RestClient:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               **bearer(cfg, "token", service="GitHub")}
    return H.RestClient("GitHub", api_base(cfg), headers=headers)


async def _gh(ctx: ToolContext) -> tuple[H.RestClient, dict[str, Any]]:
    cfg = await ctx.connector("github")
    return gh_client(cfg), cfg


def repo_path(cfg: dict[str, Any], repo: str | None) -> str:
    r = (repo or str(cfg.get("default_repo") or "")).strip()
    if r.startswith("http"):
        parts = [p for p in urlparse(r).path.split("/") if p]
        if len(parts) < 2:
            raise ToolError("Couldn't read 'owner/name' from that GitHub URL.")
        r = f"{parts[0]}/{parts[1]}"
    r = r.removesuffix(".git").strip("/")
    if "/" not in r:
        owner = str(cfg.get("default_owner") or "").strip()
        if owner and r:
            r = f"{owner}/{r}"
        else:
            raise ToolError("Pass repo as 'owner/name' (or set a default owner in the GitHub connector).")
    owner, name = r.split("/")[:2]
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


def _user(u: dict | None) -> str | None:
    return (u or {}).get("login")


def _issue(i: dict) -> dict:
    return H.compact({
        "number": i.get("number"), "title": i.get("title"), "state": i.get("state"),
        "labels": [lb.get("name") for lb in i.get("labels") or [] if isinstance(lb, dict)] or None,
        "assignees": [_user(a) for a in i.get("assignees") or []] or None, "author": _user(i.get("user")),
        "comments": i.get("comments") or None, "updated": i.get("updated_at"), "url": i.get("html_url"),
        "is_pr": True if i.get("pull_request") else None,
    })


def _pr(p: dict) -> dict:
    return H.compact({
        "number": p.get("number"), "title": p.get("title"), "state": p.get("state"), "draft": p.get("draft") or None,
        "merged": True if p.get("merged_at") else None, "author": _user(p.get("user")),
        "head": (p.get("head") or {}).get("ref"), "base": (p.get("base") or {}).get("ref"),
        "updated": p.get("updated_at"), "url": p.get("html_url"),
    })


def _lim(n: int, hi: int = 100) -> int:
    return max(1, min(int(n or 30), hi))


group = ToolGroup(
    "github", "GitHub",
    "Search code and repositories; read and manage issues and pull requests; review and merge PRs; inspect GitHub "
    "Actions runs and logs; manage releases.",
    prefix="github", icon="github", category="engineering", connector_kinds=("github",),
)


# --------------------------------------------------------------------------- search & repos
@group.tool(risk=Risk.LOW, scope="read")
async def search_code(ctx: ToolContext, query: str, repo: str | None = None, language: str | None = None,
                      limit: int = 20) -> dict:
    """Search code across GitHub (or one repository) and return matching files with snippets.

    Args:
        query: Code search terms, e.g. "def refresh_token" or "TODO path:src".
        repo: Limit to "owner/name".
        language: Limit to a language, e.g. "python".
        limit: Maximum results (1-100).
    """
    api, cfg = await _gh(ctx)
    q = query.strip()
    if repo:
        q += " repo:" + repo_path(cfg, repo).removeprefix("/repos/").replace("%2F", "/")
    if language:
        q += f" language:{language}"
    data = await api.get("/search/code", params={"q": q, "per_page": _lim(limit)},
                         headers={"Accept": "application/vnd.github.text-match+json"}) or {}
    items = []
    for it in data.get("items", []):
        items.append(H.compact({
            "repo": (it.get("repository") or {}).get("full_name"), "path": it.get("path"), "url": it.get("html_url"),
            "matches": [H.clip(m.get("fragment"), 300) for m in (it.get("text_matches") or [])[:3]] or None}))
    return {"total": data.get("total_count"), "count": len(items), "results": items}


@group.tool(risk=Risk.LOW, scope="read")
async def search_repositories(ctx: ToolContext, query: str, limit: int = 20,
                              sort: Literal["best-match", "stars", "updated"] = "best-match") -> dict:
    """Search repositories (e.g. "org:acme payments", "topic:llm language:python").

    Args:
        query: GitHub repository search query.
        limit: Maximum results.
        sort: Sort order.
    """
    api, _ = await _gh(ctx)
    data = await api.get("/search/repositories", params={
        "q": query, "per_page": _lim(limit), "sort": None if sort == "best-match" else sort}) or {}
    rows = [H.compact({"repo": r.get("full_name"), "description": H.clip(r.get("description"), 200),
                       "private": r.get("private") or None, "stars": r.get("stargazers_count"),
                       "language": r.get("language"), "updated": r.get("pushed_at"), "url": r.get("html_url")})
            for r in data.get("items", [])]
    return {"total": data.get("total_count"), "count": len(rows), "repositories": rows}


@group.tool(risk=Risk.LOW, scope="read")
async def get_repo(ctx: ToolContext, repo: str, include_readme: bool = True) -> dict:
    """Get a repository's overview: description, default branch, visibility, activity, topics and README excerpt.

    Args:
        repo: "owner/name" (or a GitHub URL).
        include_readme: Include the first part of the README.
    """
    api, cfg = await _gh(ctx)
    path = repo_path(cfg, repo)
    r = await api.get(path)
    out = H.compact({
        "repo": r.get("full_name"), "description": r.get("description"), "default_branch": r.get("default_branch"),
        "visibility": r.get("visibility") or ("private" if r.get("private") else "public"),
        "language": r.get("language"), "topics": r.get("topics") or None, "stars": r.get("stargazers_count"),
        "forks": r.get("forks_count"), "open_issues": r.get("open_issues_count"), "pushed": r.get("pushed_at"),
        "archived": r.get("archived") or None, "url": r.get("html_url"), "homepage": r.get("homepage"),
        "license": (r.get("license") or {}).get("spdx_id"),
    })
    if include_readme:
        try:
            readme = await api.get(f"{path}/readme", headers={"Accept": "application/vnd.github.raw+json"})
            if isinstance(readme, str):
                out["readme"] = H.clip(readme, 2000)
        except H.ApiError as e:
            if e.status != 404:
                raise
    return out


# --------------------------------------------------------------------------- issues
@group.tool(risk=Risk.LOW, scope="read")
async def list_issues(ctx: ToolContext, repo: str, state: Literal["open", "closed", "all"] = "open",
                      labels: list[str] | None = None, assignee: str | None = None, query: str | None = None,
                      limit: int = 30, page: int = 1) -> dict:
    """List issues in a repository (pull requests excluded), most recently updated first.

    Args:
        repo: "owner/name".
        state: open, closed or all.
        labels: Only issues with all of these labels.
        assignee: Only issues assigned to this login ("none" for unassigned, "*" for any).
        query: Optional search words (uses GitHub issue search).
        limit: Page size (1-100).
        page: Page number.
    """
    api, cfg = await _gh(ctx)
    path = repo_path(cfg, repo)
    if query:
        full = path.removeprefix("/repos/").replace("%2F", "/")
        q = f"repo:{full} is:issue {query}" + ("" if state == "all" else f" state:{state}")
        q += "".join(f' label:"{lb}"' for lb in H.as_list(labels)) + (f" assignee:{assignee}" if assignee else "")
        data = await api.get("/search/issues", params={"q": q, "per_page": _lim(limit), "page": page,
                                                       "sort": "updated"}) or {}
        rows = [_issue(i) for i in data.get("items", [])]
        return {"repo": full, "total": data.get("total_count"), "count": len(rows), "issues": rows}
    data = await api.get(f"{path}/issues", params={
        "state": state, "labels": ",".join(H.as_list(labels)) or None, "assignee": assignee,
        "per_page": _lim(limit), "page": page, "sort": "updated", "direction": "desc"}) or []
    rows = [_issue(i) for i in data if not i.get("pull_request")]
    return {"repo": path.removeprefix("/repos/"), "page": page, "count": len(rows), "issues": rows}


@group.tool(risk=Risk.LOW, scope="read")
async def get_issue(ctx: ToolContext, repo: str, number: int, comments_limit: int = 20) -> dict:
    """Read an issue (or PR conversation) with its body and latest comments.

    Args:
        repo: "owner/name".
        number: Issue number.
        comments_limit: Maximum comments to include (latest).
    """
    api, cfg = await _gh(ctx)
    path = repo_path(cfg, repo)
    i = await api.get(f"{path}/issues/{int(number)}")
    out = _issue(i)
    out.update(H.compact({"body": H.clip(i.get("body"), 6000), "created": i.get("created_at"),
                          "closed": i.get("closed_at"), "milestone": (i.get("milestone") or {}).get("title"),
                          "state_reason": i.get("state_reason")}))
    if i.get("comments") and comments_limit > 0:
        total = int(i["comments"])
        page = max(1, (total + 99) // 100)  # last page holds the latest comments
        comments = await api.get(f"{path}/issues/{int(number)}/comments", params={"per_page": 100, "page": page}) or []
        out["comments_list"] = [{"author": _user(c.get("user")), "created": c.get("created_at"),
                                 "body": H.clip(c.get("body"), 1500)} for c in comments[-int(comments_limit):]]
    return out


def _issue_preview(title: str):
    def render(a: dict) -> str:
        return H.preview(title, [("Repository", a.get("repo")), ("Issue", a.get("number")), ("Title", a.get("title")),
                                 ("State", a.get("state")), ("Labels", H.as_list(a.get("labels"))),
                                 ("Assignees", H.as_list(a.get("assignees")))],
                         a.get("body") if a.get("body") is not None else None, body_label="Body")
    return render


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("title", "body"), preview=_issue_preview("Create GitHub issue"))
async def create_issue(ctx: ToolContext, repo: str, title: str, body: str | None = None,
                       labels: list[str] | None = None, assignees: list[str] | None = None,
                       milestone: int | None = None) -> dict:
    """Open a new issue.

    Args:
        repo: "owner/name".
        title: Issue title.
        body: Markdown description.
        labels: Label names.
        assignees: GitHub logins to assign.
        milestone: Milestone number.
    """
    api, cfg = await _gh(ctx)
    body_json = H.compact({"title": title, "body": body, "labels": H.as_list(labels) or None,
                           "assignees": H.as_list(assignees) or None, "milestone": milestone})
    i = await api.post(f"{repo_path(cfg, repo)}/issues", json=body_json)
    return {"created": True, "number": i.get("number"), "title": i.get("title"), "url": i.get("html_url")}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("title", "body"), preview=_issue_preview("Update GitHub issue"))
async def update_issue(ctx: ToolContext, repo: str, number: int, title: str | None = None, body: str | None = None,
                       state: Literal["open", "closed"] | None = None,
                       state_reason: Literal["completed", "not_planned", "reopened"] | None = None,
                       labels: list[str] | None = None, assignees: list[str] | None = None) -> dict:
    """Edit an issue: title, body, open/close state, labels (replaced) or assignees (replaced).

    Args:
        repo: "owner/name".
        number: Issue number.
        title: New title.
        body: New body (replaces the old one).
        state: open or closed.
        state_reason: Why it was closed/reopened.
        labels: Replace labels with these.
        assignees: Replace assignees with these logins.
    """
    api, cfg = await _gh(ctx)
    patch: dict[str, Any] = {k: v for k, v in {"title": title, "body": body, "state": state,
                                               "state_reason": state_reason}.items() if v is not None}
    if labels is not None:
        patch["labels"] = H.as_list(labels)
    if assignees is not None:
        patch["assignees"] = H.as_list(assignees)
    if not patch:
        raise ToolError("Nothing to update.")
    i = await api.patch(f"{repo_path(cfg, repo)}/issues/{int(number)}", json=patch)
    return {"updated": True, **_issue(i)}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("body",),
            preview=lambda a: H.preview("Comment on GitHub issue / PR", [("Repository", a.get("repo")),
                                                                         ("Number", a.get("number"))],
                                        a.get("body", ""), body_label="Comment"))
async def comment(ctx: ToolContext, repo: str, number: int, body: str) -> dict:
    """Add a comment to an issue or pull request.

    Args:
        repo: "owner/name".
        number: Issue or PR number.
        body: Markdown comment.
    """
    api, cfg = await _gh(ctx)
    c = await api.post(f"{repo_path(cfg, repo)}/issues/{int(number)}/comments", json={"body": body})
    return {"commented": True, "id": c.get("id"), "url": c.get("html_url")}


# --------------------------------------------------------------------------- pull requests
@group.tool(risk=Risk.LOW, scope="read")
async def list_prs(ctx: ToolContext, repo: str, state: Literal["open", "closed", "all"] = "open",
                   base: str | None = None, head: str | None = None, limit: int = 30, page: int = 1) -> dict:
    """List pull requests, most recently updated first.

    Args:
        repo: "owner/name".
        state: open, closed or all.
        base: Only PRs into this base branch.
        head: Only PRs from this head ("user:branch").
        limit: Page size.
        page: Page number.
    """
    api, cfg = await _gh(ctx)
    path = repo_path(cfg, repo)
    data = await api.get(f"{path}/pulls", params={"state": state, "base": base, "head": head, "per_page": _lim(limit),
                                                  "page": page, "sort": "updated", "direction": "desc"}) or []
    rows = [_pr(p) for p in data]
    return {"repo": path.removeprefix("/repos/"), "page": page, "count": len(rows), "pull_requests": rows}


@group.tool(risk=Risk.LOW, scope="read")
async def get_pr(ctx: ToolContext, repo: str, number: int, include_files: bool = True,
                 include_patch: bool = False) -> dict:
    """Get a pull request with diff stats (additions, deletions, changed files), mergeability, reviews and checks.

    Args:
        repo: "owner/name".
        number: PR number.
        include_files: List changed files with per-file stats.
        include_patch: Include a short diff excerpt per file.
    """
    api, cfg = await _gh(ctx)
    path = repo_path(cfg, repo)
    p = await api.get(f"{path}/pulls/{int(number)}")
    out = _pr(p)
    out.update(H.compact({
        "body": H.clip(p.get("body"), 5000), "mergeable": p.get("mergeable"), "mergeable_state": p.get("mergeable_state"),
        "additions": p.get("additions"), "deletions": p.get("deletions"), "changed_files": p.get("changed_files"),
        "commits": p.get("commits"), "comments": (p.get("comments") or 0) + (p.get("review_comments") or 0),
        "created": p.get("created_at"), "merged_at": p.get("merged_at"),
        "merged_by": _user(p.get("merged_by")), "head_sha": ((p.get("head") or {}).get("sha") or "")[:12] or None,
        "requested_reviewers": [_user(u) for u in p.get("requested_reviewers") or []] or None,
    }))
    if include_files:
        files = await api.get(f"{path}/pulls/{int(number)}/files", params={"per_page": 100}) or []
        out["files"] = [H.compact({"file": f.get("filename"), "status": f.get("status"), "additions": f.get("additions"),
                                   "deletions": f.get("deletions"),
                                   "patch": H.clip(f.get("patch"), 1500) if include_patch else None}) for f in files]
    reviews = await api.get(f"{path}/pulls/{int(number)}/reviews", params={"per_page": 100}) or []
    latest: dict[str, str] = {}
    for r in reviews:
        if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"):
            latest[_user(r.get("user")) or "?"] = r["state"]
    if latest:
        out["reviews"] = latest
    sha = (p.get("head") or {}).get("sha")
    if sha:
        try:
            runs = await api.get(f"{path}/commits/{sha}/check-runs", params={"per_page": 100}) or {}
            counts: dict[str, int] = {}
            failing = []
            for cr in runs.get("check_runs", []):
                key = cr.get("conclusion") or cr.get("status") or "unknown"
                counts[key] = counts.get(key, 0) + 1
                if key in ("failure", "timed_out", "cancelled", "action_required"):
                    failing.append(cr.get("name"))
            if counts:
                out["checks"] = H.compact({"summary": counts, "failing": failing or None})
        except H.ApiError:
            pass
    return out


def _pr_preview(title: str):
    def render(a: dict) -> str:
        return H.preview(title, [("Repository", a.get("repo")), ("PR", a.get("number")), ("Title", a.get("title")),
                                 ("From branch", a.get("head")), ("Into branch", a.get("base")),
                                 ("Draft", a.get("draft") or None), ("State", a.get("state"))],
                         a.get("body") if a.get("body") is not None else None, body_label="Description")
    return render


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("title", "body"), preview=_pr_preview("Open pull request"))
async def create_pr(ctx: ToolContext, repo: str, title: str, head: str, base: str | None = None,
                    body: str | None = None, draft: bool = False) -> dict:
    """Open a pull request from an existing branch.

    Args:
        repo: "owner/name".
        title: PR title.
        head: Source branch ("branch", or "user:branch" from a fork).
        base: Target branch (default: the repository's default branch).
        body: Markdown description.
        draft: Open as a draft.
    """
    api, cfg = await _gh(ctx)
    path = repo_path(cfg, repo)
    if not base:
        base = (await api.get(path)).get("default_branch") or "main"
    p = await api.post(f"{path}/pulls", json=H.compact({"title": title, "head": head, "base": base, "body": body,
                                                        "draft": draft or None}))
    return {"created": True, **_pr(p)}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("title", "body"), preview=_pr_preview("Update pull request"))
async def update_pr(ctx: ToolContext, repo: str, number: int, title: str | None = None, body: str | None = None,
                    state: Literal["open", "closed"] | None = None, base: str | None = None) -> dict:
    """Edit a pull request's title, description, base branch, or close/reopen it.

    Args:
        repo: "owner/name".
        number: PR number.
        title: New title.
        body: New description.
        state: open or closed.
        base: New base branch.
    """
    api, cfg = await _gh(ctx)
    patch = {k: v for k, v in {"title": title, "body": body, "state": state, "base": base}.items() if v is not None}
    if not patch:
        raise ToolError("Nothing to update.")
    p = await api.patch(f"{repo_path(cfg, repo)}/pulls/{int(number)}", json=patch)
    return {"updated": True, **_pr(p)}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("body",),
            risk_fn=lambda a: Risk.HIGH if str(a.get("event") or "").upper() == "APPROVE" else Risk.MEDIUM,
            preview=lambda a: H.preview("Review pull request", [
                ("Repository", a.get("repo")), ("PR", a.get("number")), ("Verdict", a.get("event") or "COMMENT"),
                ("Line comments", len(a.get("comments") or []) or None)], a.get("body", ""), body_label="Review"))
async def review_pr(ctx: ToolContext, repo: str, number: int,
                    event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"] = "COMMENT", body: str = "",
                    comments: list[dict] | None = None) -> dict:
    """Submit a pull request review: comment, approve or request changes (optionally with line comments).

    Args:
        repo: "owner/name".
        number: PR number.
        event: COMMENT, APPROVE or REQUEST_CHANGES.
        body: Review summary (required for COMMENT and REQUEST_CHANGES).
        comments: Line comments: [{"path": "src/app.py", "line": 42, "body": "..."}] (line in the new file).
    """
    if event in ("COMMENT", "REQUEST_CHANGES") and not body.strip() and not comments:
        raise ToolError("A review body is required for COMMENT and REQUEST_CHANGES.")
    api, cfg = await _gh(ctx)
    payload: dict[str, Any] = {"event": event, "body": body or None}
    if comments:
        payload["comments"] = [H.compact({"path": c.get("path"), "line": c.get("line"), "side": c.get("side", "RIGHT"),
                                          "body": c.get("body")}) for c in comments if c.get("path") and c.get("body")]
    r = await api.post(f"{repo_path(cfg, repo)}/pulls/{int(number)}/reviews", json=H.compact(payload))
    return {"reviewed": True, "state": r.get("state"), "id": r.get("id"), "url": r.get("html_url")}


@group.tool(risk=Risk.CRITICAL, scope="write", content_fields=("commit_title", "commit_message"),
            preview=lambda a: H.preview("MERGE pull request", [
                ("Repository", a.get("repo")), ("PR", a.get("number")), ("Method", a.get("method") or "squash"),
                ("Commit title", a.get("commit_title")), ("Expected head SHA", a.get("sha"))],
                a.get("commit_message") if a.get("commit_message") else None, body_label="Commit message",
                note="Merging changes the target branch and may trigger deployments."))
async def merge_pr(ctx: ToolContext, repo: str, number: int, method: Literal["squash", "merge", "rebase"] = "squash",
                   commit_title: str | None = None, commit_message: str | None = None, sha: str | None = None) -> dict:
    """Merge a pull request.

    Args:
        repo: "owner/name".
        number: PR number.
        method: squash, merge or rebase.
        commit_title: Title for the merge/squash commit.
        commit_message: Extra detail for the merge commit.
        sha: Only merge if the PR head is still this SHA (protects against late pushes).
    """
    api, cfg = await _gh(ctx)
    r = await api.put(f"{repo_path(cfg, repo)}/pulls/{int(number)}/merge", json=H.compact({
        "merge_method": method, "commit_title": commit_title, "commit_message": commit_message, "sha": sha}))
    return {"merged": bool(r.get("merged")), "sha": r.get("sha"), "message": r.get("message")}


# --------------------------------------------------------------------------- Actions
def _run(r: dict) -> dict:
    return H.compact({
        "id": r.get("id"), "workflow": r.get("name"), "title": H.clip(r.get("display_title"), 120),
        "event": r.get("event"), "status": r.get("status"), "conclusion": r.get("conclusion"),
        "branch": r.get("head_branch"), "sha": (r.get("head_sha") or "")[:7] or None, "run_number": r.get("run_number"),
        "actor": _user(r.get("actor")), "created": r.get("created_at"), "url": r.get("html_url"),
    })


@group.tool(risk=Risk.LOW, scope="read")
async def workflow_runs(ctx: ToolContext, repo: str, workflow: str | None = None, branch: str | None = None,
                        status: str | None = None, limit: int = 20) -> dict:
    """List recent GitHub Actions workflow runs.

    Args:
        repo: "owner/name".
        workflow: Workflow file name (e.g. "ci.yml") or id to filter by.
        branch: Only runs for this branch.
        status: queued, in_progress, completed, success, failure, cancelled...
        limit: Maximum runs.
    """
    api, cfg = await _gh(ctx)
    path = repo_path(cfg, repo)
    url = f"{path}/actions/workflows/{quote(workflow, safe='')}/runs" if workflow else f"{path}/actions/runs"
    data = await api.get(url, params={"branch": branch, "status": status, "per_page": _lim(limit)}) or {}
    rows = [_run(r) for r in data.get("workflow_runs", [])]
    return {"total": data.get("total_count"), "count": len(rows), "runs": rows}


@group.tool(risk=Risk.LOW, scope="read")
async def workflow_status(ctx: ToolContext, repo: str, run_id: int) -> dict:
    """Get a workflow run's status with its jobs and the steps that failed.

    Args:
        repo: "owner/name".
        run_id: Run id from github_workflow_runs.
    """
    api, cfg = await _gh(ctx)
    path = repo_path(cfg, repo)
    run = await api.get(f"{path}/actions/runs/{int(run_id)}")
    jobs = await api.get(f"{path}/actions/runs/{int(run_id)}/jobs", params={"per_page": 100}) or {}
    out = _run(run)
    out["jobs"] = [H.compact({
        "id": j.get("id"), "name": j.get("name"), "status": j.get("status"), "conclusion": j.get("conclusion"),
        "started": j.get("started_at"), "completed": j.get("completed_at"),
        "failed_steps": [s.get("name") for s in j.get("steps") or [] if s.get("conclusion") == "failure"] or None,
    }) for j in jobs.get("jobs", [])]
    return out


def _log_excerpt(text: str, budget: int) -> str:
    text = _TS_PREFIX.sub("", text or "")
    lines = text.splitlines()
    errs = [i for i, ln in enumerate(lines) if "##[error]" in ln or re.search(r"\b(Error|FAILED|Traceback)\b", ln)]
    if errs:
        chunk = "\n".join(lines[max(0, errs[0] - 40): min(len(lines), errs[-1] + 15)])
    else:
        chunk = "\n".join(lines[-200:])
    return chunk if len(chunk) <= budget else "…" + chunk[-budget:]


@group.tool(risk=Risk.LOW, scope="read")
async def workflow_logs(ctx: ToolContext, repo: str, run_id: int | None = None, job_id: int | None = None,
                        max_chars: int = 12000, failed_only: bool = True) -> dict:
    """Fetch the logs of a workflow job (or the failed jobs of a run), trimmed to the part around the errors.

    Args:
        repo: "owner/name".
        run_id: Run id — logs of its failed jobs (or all jobs when failed_only is false).
        job_id: A specific job id instead.
        max_chars: Total characters of log to return.
        failed_only: With run_id, only fetch failed jobs.
    """
    if not run_id and not job_id:
        raise ToolError("Pass run_id or job_id.")
    api, cfg = await _gh(ctx)
    path = repo_path(cfg, repo)
    if job_id:
        jobs = [{"id": job_id, "name": f"job {job_id}"}]
    else:
        data = await api.get(f"{path}/actions/runs/{int(run_id)}/jobs", params={"per_page": 100}) or {}
        all_jobs = data.get("jobs", [])
        jobs = [j for j in all_jobs if j.get("conclusion") in ("failure", "timed_out", "cancelled")] or (
            [] if failed_only else all_jobs)
        if not jobs:
            return {"run_id": run_id, "jobs": [], "note": "No failed jobs in this run (pass failed_only=false for all)."}
        jobs = jobs[:3]
    budget = max(1000, min(int(max_chars or 12000), 60000)) // len(jobs)
    out = []
    for j in jobs:
        raw, _ = await api.fetch(f"{path}/actions/jobs/{int(j['id'])}/logs", max_bytes=30 * 1024 * 1024)
        text = raw.decode("utf-8", "replace")
        out.append({"job_id": j["id"], "name": j.get("name"), "conclusion": j.get("conclusion"),
                    "log": _log_excerpt(text, budget), "log_chars_total": len(text)})
    return {"run_id": run_id, "jobs": out, "note": "Logs are trimmed to the region around errors (or the tail)."}


# --------------------------------------------------------------------------- releases & tags
@group.tool(risk=Risk.LOW, scope="read")
async def list_releases(ctx: ToolContext, repo: str, limit: int = 10) -> dict:
    """List releases (newest first) with tag, name, status and notes excerpt.

    Args:
        repo: "owner/name".
        limit: Maximum releases.
    """
    api, cfg = await _gh(ctx)
    data = await api.get(f"{repo_path(cfg, repo)}/releases", params={"per_page": _lim(limit)}) or []
    rows = [H.compact({"id": r.get("id"), "tag": r.get("tag_name"), "name": r.get("name"),
                       "draft": r.get("draft") or None, "prerelease": r.get("prerelease") or None,
                       "published": r.get("published_at"), "author": _user(r.get("author")),
                       "url": r.get("html_url"), "notes": H.clip(r.get("body"), 600)}) for r in data]
    return {"count": len(rows), "releases": rows}


@group.tool(risk=Risk.LOW, scope="read")
async def list_tags(ctx: ToolContext, repo: str, limit: int = 30) -> dict:
    """List git tags.

    Args:
        repo: "owner/name".
        limit: Maximum tags.
    """
    api, cfg = await _gh(ctx)
    data = await api.get(f"{repo_path(cfg, repo)}/tags", params={"per_page": _lim(limit)}) or []
    return {"count": len(data), "tags": [{"name": t.get("name"), "sha": ((t.get("commit") or {}).get("sha") or "")[:7]}
                                         for t in data]}


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("name", "body"),
            preview=lambda a: H.preview("Publish GitHub release" if not a.get("draft") else "Create draft GitHub release", [
                ("Repository", a.get("repo")), ("Tag", a.get("tag")), ("Target", a.get("target") or "default branch"),
                ("Name", a.get("name")), ("Draft", bool(a.get("draft"))), ("Pre-release", bool(a.get("prerelease"))),
                ("Auto-generate notes", bool(a.get("generate_notes")))],
                a.get("body") if a.get("body") else None, body_label="Release notes"))
async def create_release(ctx: ToolContext, repo: str, tag: str, name: str | None = None, body: str | None = None,
                         target: str | None = None, draft: bool = False, prerelease: bool = False,
                         generate_notes: bool = False) -> dict:
    """Create a release (and its tag if it doesn't exist yet).

    Args:
        repo: "owner/name".
        tag: Tag name, e.g. "v1.4.0".
        name: Release title.
        body: Release notes (Markdown).
        target: Branch or commit SHA to tag (default: the default branch).
        draft: Save as a draft (not published).
        prerelease: Mark as a pre-release.
        generate_notes: Let GitHub generate notes from merged PRs.
    """
    api, cfg = await _gh(ctx)
    r = await api.post(f"{repo_path(cfg, repo)}/releases", json=H.compact({
        "tag_name": tag, "name": name, "body": body, "target_commitish": target, "draft": draft or None,
        "prerelease": prerelease or None, "generate_release_notes": generate_notes or None}))
    return {"created": True, "id": r.get("id"), "tag": r.get("tag_name"), "draft": r.get("draft"),
            "url": r.get("html_url")}


# --------------------------------------------------------------------------- connector kind
@H.connector_test
async def _test_github(cfg: dict) -> tuple[bool, str]:
    api = gh_client(cfg)
    resp = await api.request("GET", "/user", raw=True)
    me = resp.json()
    scopes = resp.headers.get("x-oauth-scopes")
    return True, f"Connected to GitHub as {me.get('login')}" + (f" (scopes: {scopes})." if scopes else ".")


register_connector_kind(ConnectorKindSpec(
    kind="github", name="GitHub", description="Repositories, issues, pull requests, Actions and releases.",
    icon="github", category="engineering", tool_groups=["github"],
    fields=[
        ConnectorField("token", "Personal access token", type="password", required=True, secret=True,
                       placeholder="github_pat_… or ghp_…",
                       help="Fine-grained token with Contents, Issues, Pull requests, Actions (read) and Metadata "
                            "permissions on the repositories the agents may use."),
        ConnectorField("base_url", "GitHub Enterprise URL (optional)", type="url",
                       placeholder="https://github.example.com",
                       help="Leave empty for github.com. For GitHub Enterprise Server the /api/v3 suffix is added."),
        ConnectorField("default_owner", "Default owner / organization (optional)", placeholder="acme",
                       help="Lets agents refer to repositories by name only."),
    ],
    docs_url="https://docs.github.com/rest/authentication/authenticating-to-the-rest-api", test=_test_github,
))
